"""Agent Détective : détection déterministe d'anomalies (volume, montants, fraîcheur, prix,
doublons, catégories). Chaque anomalie embarque la requête qui la prouve."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .config import Settings, get_settings
from .models import Anomaly, Evidence
from .warehouse import Warehouse

ORDERS_REQUIRED = {"order_id", "order_date", "amount", "status"}

DAILY_SQL = """
SELECT CAST(order_date AS DATE) AS day,
       count(*) AS orders,
       coalesce(sum(amount) FILTER (WHERE status <> 'cancelled'), 0) AS revenue
FROM orders
WHERE CAST(order_date AS DATE) BETWEEN ? AND ?
GROUP BY 1 ORDER BY 1
"""


@dataclass
class DetectionContext:
    as_of: date
    window_start: date
    history_start: date
    settings: Settings


def columns_of(wh: Warehouse, table: str) -> set[str]:
    if not wh.has_table(table):
        return set()
    return set(wh.query(f'DESCRIBE "{table}"')["column_name"])


def resolve_as_of(wh: Warehouse) -> date:
    """Date de référence du tableau de bord : dernier lot attendu, sinon dernière commande."""
    if {"table_name", "batch_date"} <= columns_of(wh, "pipeline_logs"):
        df = wh.query("SELECT max(CAST(batch_date AS DATE)) AS d FROM pipeline_logs WHERE table_name = 'orders'")
        if pd.notna(df["d"].iloc[0]):
            return pd.Timestamp(df["d"].iloc[0]).date()
    df = wh.query("SELECT max(CAST(order_date AS DATE)) AS d FROM orders")
    return pd.Timestamp(df["d"].iloc[0]).date()


def make_context(wh: Warehouse, settings: Settings | None = None, as_of: date | None = None) -> DetectionContext:
    settings = settings or get_settings()
    as_of = as_of or resolve_as_of(wh)
    first = pd.Timestamp(wh.query("SELECT min(CAST(order_date AS DATE)) AS d FROM orders")["d"].iloc[0]).date()
    return DetectionContext(as_of, as_of - timedelta(days=settings.detection_window_days - 1), first, settings)


def daily_metrics(wh: Warehouse, start: date, end: date) -> pd.DataFrame:
    df = wh.query(DAILY_SQL, [start, end])
    df["day"] = pd.to_datetime(df["day"])
    idx = pd.date_range(start, end, freq="D", name="day")
    return df.set_index("day").reindex(idx, fill_value=0).astype(float)


def with_baseline(daily: pd.DataFrame) -> pd.DataFrame:
    """Référence = médiane du même jour de semaine sur les 4 semaines précédentes."""
    out = daily.copy()
    for metric in ("orders", "revenue"):
        lags = pd.concat([daily[metric].shift(7 * k) for k in range(1, 5)], axis=1)
        out[f"{metric}_expected"] = lags.median(axis=1, skipna=False)
        out[f"{metric}_dev"] = (out[metric] - out[f"{metric}_expected"]) / out[f"{metric}_expected"]
    return out


def _robust_scale(devs: pd.Series) -> float:
    devs = devs.dropna()
    if len(devs) < 10:
        return 0.0
    return float(1.4826 * np.median(np.abs(devs - devs.median())))


def detect_daily(wh: Warehouse, ctx: DetectionContext) -> list[Anomaly]:
    data = with_baseline(daily_metrics(wh, ctx.history_start, ctx.as_of))
    history = data[data.index < pd.Timestamp(ctx.window_start)]
    window = data[data.index >= pd.Timestamp(ctx.window_start)]
    anomalies: list[Anomaly] = []
    labels = {"orders": ("volume", "Nombre de commandes", ["order_id"]),
              "revenue": ("revenue", "Chiffre d'affaires", ["amount", "status"])}
    for metric, (kind, label, cols) in labels.items():
        threshold = max(ctx.settings.min_relative_deviation,
                        ctx.settings.robust_z_threshold * _robust_scale(history[f"{metric}_dev"]))
        for day, row in window.iterrows():
            dev = row[f"{metric}_dev"]
            if pd.isna(dev) or abs(dev) < threshold:
                continue
            d = day.date().isoformat()
            direction = "drop" if dev < 0 else "spike"
            ev = Evidence.from_query(
                f"{label} du {d} comparé à la médiane des 4 mêmes jours de semaine précédents "
                f"(seuil adaptatif ±{threshold:.0%}).",
                DAILY_SQL.replace("?", f"DATE '{(day - pd.Timedelta(days=28)).date()}'", 1).replace("?", f"DATE '{d}'", 1),
                data.loc[[day - pd.Timedelta(days=7 * k) for k in range(4, -1, -1) if day - pd.Timedelta(days=7 * k) in data.index],
                         ["orders", "revenue"]].reset_index())
            anomalies.append(Anomaly(
                id=f"{kind}_{direction}_{d}", kind=f"{kind}_{direction}", table="orders", columns=cols,
                period_start=d, period_end=d, metric=metric, observed=round(float(row[metric]), 2),
                expected=round(float(row[f"{metric}_expected"]), 2), deviation=round(float(dev), 4),
                severity="high" if abs(dev) >= 0.5 else "medium",
                summary=f"{label} du {d} : {row[metric]:,.0f} contre {row[f'{metric}_expected']:,.0f} attendu ({dev:+.0%}).",
                evidence=ev))
    return anomalies


def detect_freshness(wh: Warehouse, ctx: DetectionContext) -> list[Anomaly]:
    sql = "SELECT max(CAST(order_date AS DATE)) AS last_order_date, max(loaded_at) AS last_loaded_at FROM orders" \
        if "loaded_at" in columns_of(wh, "orders") else "SELECT max(CAST(order_date AS DATE)) AS last_order_date FROM orders"
    df = wh.query(sql)
    last = pd.Timestamp(df["last_order_date"].iloc[0]).date()
    lag = (ctx.as_of - last).days
    if lag < 1:
        return []
    missing_from = (last + timedelta(days=1)).isoformat()
    return [Anomaly(
        id=f"freshness_{ctx.as_of.isoformat()}", kind="freshness", table="orders", columns=["order_date", "loaded_at"],
        period_start=missing_from, period_end=ctx.as_of.isoformat(), metric="lag_days", observed=float(lag), expected=0.0,
        deviation=None, severity="high" if lag >= 2 else "medium",
        summary=f"Données de commandes en retard de {lag} jour(s) : dernière commande le {last}, "
                f"tableau de bord daté du {ctx.as_of}.",
        evidence=Evidence.from_query("Dernière date de commande présente dans l'entrepôt.", sql, df))]


def detect_duplicates(wh: Warehouse, ctx: DetectionContext) -> list[Anomaly]:
    sql = """
    SELECT CAST(order_date AS DATE) AS day, count(*) AS rows_total, count(DISTINCT order_id) AS distinct_orders,
           count(*) - count(DISTINCT order_id) AS duplicate_rows
    FROM orders GROUP BY 1 HAVING count(*) > count(DISTINCT order_id) ORDER BY 1
    """
    df = wh.query(sql)
    out = []
    for _, row in df.iterrows():
        d = pd.Timestamp(row["day"]).date().isoformat()
        out.append(Anomaly(
            id=f"duplicate_keys_{d}", kind="duplicate_keys", table="orders", columns=["order_id"],
            period_start=d, period_end=d, metric="duplicate_rows", observed=float(row["duplicate_rows"]), expected=0.0,
            deviation=None, severity="high",
            summary=f"{int(row['duplicate_rows'])} lignes de commandes dupliquées (même order_id) le {d}.",
            evidence=Evidence.from_query("Identifiants de commande présents plusieurs fois, par jour.", sql,
                                         df[df["day"] == row["day"]])))
    return out


PRICE_SQL = """
WITH hist AS (
    SELECT product_id, ln(unit_price) AS lp FROM orders
    WHERE CAST(order_date AS DATE) < ? AND unit_price > 0
),
med AS (SELECT product_id, median(lp) AS m, count(*) AS n FROM hist GROUP BY 1),
ref AS (
    -- Seuil propre à chaque produit : au moins un facteur k, et au moins 4 écarts robustes
    -- de sa dispersion historique (évite d'alerter sur des produits aux prix naturellement variables).
    SELECT h.product_id, exp(any_value(m.m)) AS ref_price,
           greatest(ln(?), 4 * 1.4826 * median(abs(h.lp - m.m))) AS log_threshold
    FROM hist h JOIN med m USING (product_id) WHERE m.n >= 5 GROUP BY 1
)
SELECT o.product_id, min(CAST(o.order_date AS DATE)) AS first_seen, max(CAST(o.order_date AS DATE)) AS last_seen,
       count(*) AS outlier_orders, median(o.unit_price) AS observed_price, any_value(r.ref_price) AS reference_price,
       median(o.unit_price) / any_value(r.ref_price) AS ratio, exp(any_value(r.log_threshold)) AS threshold_factor,
       coalesce(sum((o.unit_price - r.ref_price) * o.quantity) FILTER (WHERE o.status <> 'cancelled'), 0) AS excess_revenue
FROM orders o JOIN ref r USING (product_id)
WHERE CAST(o.order_date AS DATE) BETWEEN ? AND ?
  AND o.unit_price > 0 AND abs(ln(o.unit_price) - ln(r.ref_price)) > r.log_threshold
GROUP BY 1 ORDER BY excess_revenue DESC
"""


def price_outliers(wh: Warehouse, ctx: DetectionContext, start: date | None = None, end: date | None = None,
                   ratio: float | None = None) -> pd.DataFrame:
    ratio = ratio or ctx.settings.price_outlier_ratio
    return wh.query(PRICE_SQL, [ctx.window_start, ratio, start or ctx.window_start, end or ctx.as_of])


def detect_prices(wh: Warehouse, ctx: DetectionContext) -> list[Anomaly]:
    df = price_outliers(wh, ctx)
    out = []
    for _, row in df.iterrows():
        pid = row["product_id"]
        first, last = (pd.Timestamp(row[c]).date().isoformat() for c in ("first_seen", "last_seen"))
        out.append(Anomaly(
            id=f"price_outlier_{pid}", kind="price_outlier", table="orders", columns=["unit_price", "amount"],
            period_start=first, period_end=last, metric="unit_price", observed=round(float(row["observed_price"]), 2),
            expected=round(float(row["reference_price"]), 2), deviation=round(float(row["ratio"]) - 1, 4),
            severity="high",
            summary=f"Prix unitaire de {pid} à {row['observed_price']:,.2f} € contre {row['reference_price']:,.2f} € "
                    f"historiquement (×{row['ratio']:.1f}) sur {int(row['outlier_orders'])} commandes "
                    f"du {first} au {last}.",
            evidence=Evidence.from_query(
                f"Prix unitaires s'écartant de la médiane historique du produit de plus de ×{row['threshold_factor']:.1f} "
                f"(seuil propre au produit, ≥ ×{ctx.settings.price_outlier_ratio:g} ; historique antérieur au {ctx.window_start}).",
                PRICE_SQL, df[df["product_id"] == pid])))
    return out


def detect_categories(wh: Warehouse, ctx: DetectionContext) -> list[Anomaly]:
    if "category" not in columns_of(wh, "products"):
        return []
    sql = """
    SELECT count(*) FILTER (WHERE p.category IS NULL) AS products_without_category,
           count(*) AS products_total
    FROM products p
    """
    df = wh.query(sql)
    n_null = int(df["products_without_category"].iloc[0])
    if n_null == 0:
        return []
    share_sql = """
    SELECT coalesce(sum(o.amount) FILTER (WHERE p.category IS NULL), 0) AS uncategorized_revenue,
           sum(o.amount) AS revenue
    FROM orders o LEFT JOIN products p USING (product_id)
    WHERE o.status <> 'cancelled' AND CAST(o.order_date AS DATE) BETWEEN ? AND ?
    """
    share_df = wh.query(share_sql, [ctx.window_start, ctx.as_of])
    share = float(share_df["uncategorized_revenue"].iloc[0] / share_df["revenue"].iloc[0]) if share_df["revenue"].iloc[0] else 0.0
    return [Anomaly(
        id="missing_category_products", kind="missing_category", table="products", columns=["category"],
        period_start=ctx.window_start.isoformat(), period_end=ctx.as_of.isoformat(), metric="null_categories",
        observed=float(n_null), expected=0.0, deviation=round(share, 4),
        severity="high" if share >= 0.05 else "medium",
        summary=f"{n_null} produit(s) sur {int(df['products_total'].iloc[0])} sans catégorie, "
                f"représentant {share:.1%} du CA des {ctx.settings.detection_window_days} derniers jours.",
        evidence=Evidence.from_query("Produits sans catégorie et part du CA concernée.", sql + ";" + share_sql,
                                     pd.concat([df, share_df], axis=1)))]


# Règles d'intégrité métier : chaque règle ne s'applique que si les colonnes requises existent.
# "{as_of}" est remplacé par la date d'analyse.
INTEGRITY_RULES: dict[str, dict] = {
    "amount_missing": {"label": "Chiffre d'affaires manquant", "table": "orders", "requires": {"amount"},
                       "where": "amount IS NULL", "columns": ["amount"]},
    "amount_negative": {"label": "Chiffre d'affaires négatif", "table": "orders", "requires": {"amount"},
                        "where": "amount < 0", "columns": ["amount"]},
    "quantity_non_positive": {"label": "Quantité nulle ou négative", "table": "orders", "requires": {"quantity"},
                              "where": "quantity <= 0", "columns": ["quantity"]},
    "cost_negative": {"label": "Coût négatif", "table": "orders", "requires": {"cost"},
                      "where": "cost < 0", "columns": ["cost"]},
    "margin_inconsistent": {"label": "Marge différente de CA − coût", "table": "orders",
                            "requires": {"margin", "amount", "cost"},
                            "where": "abs(margin - (amount - cost)) > 0.01", "columns": ["margin"]},
    "order_date_missing": {"label": "Vente sans date valide (clé de date orpheline)", "table": "orders",
                           "requires": {"order_date"}, "where": "order_date IS NULL", "columns": ["order_date"]},
    "product_orphan": {"label": "Produit absent du référentiel", "table": "orders", "requires": {"product_id"},
                       "needs_table": "products",
                       "where": "product_id IS NULL OR product_id NOT IN (SELECT product_id FROM products)",
                       "columns": ["product_id"]},
    "customer_orphan": {"label": "Client absent du référentiel", "table": "orders", "requires": {"customer_id"},
                        "needs_table": "customers",
                        "where": "customer_id IS NULL OR customer_id NOT IN (SELECT customer_id FROM customers)",
                        "columns": ["customer_id"]},
    "signup_in_future": {"label": "Date d'inscription client postérieure à la date d'analyse", "table": "customers",
                         "requires": {"signup_date"}, "where": "CAST(signup_date AS DATE) > DATE '{as_of}'",
                         "columns": ["signup_date"]},
}


def integrity_where(rule_id: str, as_of: date) -> str:
    return INTEGRITY_RULES[rule_id]["where"].replace("{as_of}", as_of.isoformat())


def applicable_rules(wh: Warehouse) -> list[str]:
    out = []
    for rule_id, rule in INTEGRITY_RULES.items():
        if rule["requires"] <= columns_of(wh, rule["table"]) and (
                "needs_table" not in rule or wh.has_table(rule["needs_table"])):
            out.append(rule_id)
    return out


def detect_integrity(wh: Warehouse, ctx: DetectionContext) -> list[Anomaly]:
    out = []
    for rule_id in applicable_rules(wh):
        rule = INTEGRITY_RULES[rule_id]
        where = integrity_where(rule_id, ctx.as_of)
        table = rule["table"]
        count_sql = f'SELECT count(*) FILTER (WHERE {where}) AS violations, count(*) AS total FROM "{table}"'
        counts = wh.query(count_sql)
        n, total = int(counts["violations"].iloc[0]), int(counts["total"].iloc[0])
        if n == 0:
            continue
        sample_sql = f'SELECT * FROM "{table}" WHERE {where} LIMIT 10'
        sample = wh.query(sample_sql)
        share = n / total if total else 0.0
        if table == "orders" and "order_date" in columns_of(wh, "orders"):
            span = wh.query(f"SELECT min(CAST(order_date AS DATE)) AS a, max(CAST(order_date AS DATE)) AS b FROM orders WHERE {where}")
            start, end = (span["a"].iloc[0], span["b"].iloc[0])
            start = pd.Timestamp(start).date().isoformat() if pd.notna(start) else ctx.history_start.isoformat()
            end = pd.Timestamp(end).date().isoformat() if pd.notna(end) else ctx.as_of.isoformat()
        else:
            start, end = ctx.history_start.isoformat(), ctx.as_of.isoformat()
        out.append(Anomaly(
            id=f"integrity_{rule_id}", kind="integrity", table=table, columns=rule["columns"],
            period_start=start, period_end=end, metric=rule_id, observed=float(n), expected=0.0,
            deviation=round(share, 6), severity="high" if share >= 0.01 else "medium",
            summary=f"{rule['label']} : {n:,} ligne(s) de {table} ({share:.2%}).".replace(",", " "),
            evidence=Evidence.from_query(f"Règle d'intégrité « {rule['label']} » ({where}).",
                                         count_sql + "; " + sample_sql, sample, limit=10)))
    return out


DETECTORS = [detect_daily, detect_freshness, detect_duplicates, detect_prices, detect_categories, detect_integrity]

# Familles de contrôles proposées à l'analyste (cases à cocher de l'interface).
# Les doublons relèvent du volume ; les règles d'intégrité ligne à ligne, des montants.
CHECKS = {"volume": "Volume", "freshness": "Fraîcheur", "amounts": "Montants", "categories": "Catégories"}


def check_of(kind: str) -> str:
    if kind.startswith("volume_") or kind == "duplicate_keys":
        return "volume"
    if kind == "freshness":
        return "freshness"
    if kind == "missing_category":
        return "categories"
    return "amounts"  # revenue_*, price_outlier, integrity


def run_detection(wh: Warehouse, settings: Settings | None = None, as_of: date | None = None) -> dict:
    """Exécute tous les détecteurs compatibles avec le schéma disponible."""
    if not ORDERS_REQUIRED <= columns_of(wh, "orders"):
        return {"as_of": None, "anomalies": [], "skipped": [
            f"Table 'orders' absente ou incomplète (colonnes requises : {sorted(ORDERS_REQUIRED)})."]}
    ctx = make_context(wh, settings, as_of)
    anomalies, skipped = [], []
    for detector in DETECTORS:
        try:
            anomalies.extend(detector(wh, ctx))
        except Exception as exc:  # un détecteur incompatible ne bloque pas les autres
            skipped.append(f"{detector.__name__} : {exc}")
    return {
        "as_of": ctx.as_of.isoformat(),
        "window_start": ctx.window_start.isoformat(),
        "anomalies": [a.to_dict() for a in anomalies],
        "skipped": skipped,
    }
