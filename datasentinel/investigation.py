"""Agent Enquêteur : regroupe les anomalies en dossiers, formule des hypothèses de cause et
les teste par des requêtes déterministes. Le résultat d'un test dépend uniquement des données."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from .detection import (INTEGRITY_RULES, PRICE_SQL, DetectionContext, columns_of, daily_metrics, integrity_where,
                        price_outliers, with_baseline)
from .models import INCONCLUSIVE, REFUTED, SUPPORTED, UNLIKELY, UNTESTABLE, Evidence, Hypothesis
from .warehouse import Warehouse

LATE_STATUSES = ("delayed", "failed", "pending", "running", "error")
EXPLAINED_ENOUGH = 0.8


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _share(effect: float, gap: float) -> float | None:
    if not gap:
        return None
    return round(max(0.0, min(1.0, effect / gap)), 4)


class Investigator:
    def __init__(self, wh: Warehouse, ctx: DetectionContext):
        self.wh, self.ctx = wh, ctx
        self.has_logs = {"table_name", "batch_date", "rows_loaded"} <= columns_of(wh, "pipeline_logs")
        self.has_snapshot = "category" in columns_of(wh, "products_snapshot")
        self.daily = with_baseline(daily_metrics(wh, ctx.history_start, ctx.as_of))
        hist = self.daily[self.daily.index < pd.Timestamp(ctx.window_start)]
        self.avg_order_revenue = float(hist["revenue"].sum() / hist["orders"].sum()) if hist["orders"].sum() else 0.0

    # ------------------------------------------------------------------ dossiers
    @staticmethod
    def build_cases(anomalies: list[dict]) -> list[dict]:
        cases: dict[str, dict] = {}
        for a in anomalies:
            key = f"daily_{a['period_start']}" if a["kind"].startswith(("volume_", "revenue_")) else a["id"]
            cases.setdefault(key, {"id": key, "anomalies": []})["anomalies"].append(a)
        return list(cases.values())

    def investigate(self, anomalies: list[dict]) -> list[dict]:
        return [self.investigate_case(c) for c in self.build_cases(anomalies)]

    def _gap(self, d: date, metric: str) -> float:
        row = self.daily.loc[pd.Timestamp(d)]
        exp = row[f"{metric}_expected"]
        return 0.0 if pd.isna(exp) else float(row[metric] - exp)

    def investigate_case(self, case: dict) -> dict:
        kinds = {a["kind"] for a in case["anomalies"]}
        first = case["anomalies"][0]
        hyps: list[Hypothesis] = []
        primary = "revenue" if any(k.startswith("revenue_") for k in kinds) else "orders"

        if case["id"].startswith("daily_"):
            d = _d(first["period_start"])
            gaps = {"orders": self._gap(d, "orders"), "revenue": self._gap(d, "revenue")}
            if any(k.endswith("_drop") for k in kinds):
                hyps += [self.test_partial_load(d, gaps), self.test_late_load(d, d, gaps)]
            if any(k.endswith("_spike") for k in kinds):
                hyps += [self.test_duplicate_load(d, gaps), self.test_price(d, d, gaps)]
            hyps.append(self.test_business_variation(hyps, primary, kinds))
        elif first["kind"] == "freshness":
            hyps.append(self.test_late_load(_d(first["period_start"]), _d(first["period_end"]), None))
        elif first["kind"] == "duplicate_keys":
            d = _d(first["period_start"])
            hyps.append(self.test_duplicate_load(d, {"orders": self._gap(d, "orders"), "revenue": self._gap(d, "revenue")}))
        elif first["kind"] == "price_outlier":
            hyps.append(self.test_price(_d(first["period_start"]), _d(first["period_end"]), None,
                                        product=first["id"].removeprefix("price_outlier_")))
        elif first["kind"] == "missing_category":
            hyps.append(self.test_missing_categories())
        elif first["kind"] == "integrity":
            hyps.append(self.test_integrity(first))

        supported = [h for h in hyps if h.status == SUPPORTED]
        shares = [h.explained_share for h in supported if h.explained_share is not None]
        explained = round(min(1.0, sum(shares)), 4) if shares else (1.0 if supported else 0.0)
        return {
            "id": case["id"],
            "anomalies": case["anomalies"],
            "primary_metric": primary,
            "hypotheses": [h.to_dict() for h in hyps],
            "explained_share": explained,
            "residual_share": round(1 - explained, 4),
        }

    # ------------------------------------------------------------------ tests
    def _logs(self, start: date, end: date, table: str = "orders") -> tuple[str, pd.DataFrame]:
        sql = """
        SELECT CAST(batch_date AS DATE) AS batch_date, run_id, status, rows_expected, rows_loaded,
               started_at, finished_at, error_message
        FROM pipeline_logs WHERE table_name = ? AND CAST(batch_date AS DATE) BETWEEN ? AND ?
        ORDER BY batch_date, started_at
        """
        return sql, self.wh.query(sql, [table, start, end])

    def _rows_per_day(self, start: date, end: date) -> tuple[str, pd.DataFrame]:
        sql = """
        SELECT CAST(order_date AS DATE) AS order_date, count(*) AS rows_in_table, count(DISTINCT order_id) AS distinct_orders
        FROM orders WHERE CAST(order_date AS DATE) BETWEEN ? AND ? GROUP BY 1 ORDER BY 1
        """
        return sql, self.wh.query(sql, [start, end])

    def test_partial_load(self, d: date, gaps: dict) -> Hypothesis:
        h = Hypothesis("partial_load", d.isoformat(),
                       f"Le lot de commandes du {d} n'a été chargé que partiellement.",
                       INCONCLUSIVE, ["Comparer rows_loaded à rows_expected dans le journal de chargement",
                                      "Vérifier que le nombre de lignes en table correspond aux lignes chargées"])
        if not self.has_logs:
            h.status, h.details["reason"] = UNTESTABLE, "Aucun journal de chargement (pipeline_logs) disponible."
            return h
        sql, logs = self._logs(d, d)
        cnt_sql, cnt = self._rows_per_day(d, d)
        h.evidence += [Evidence.from_query(f"Journal de chargement du lot orders du {d}.", sql, logs),
                       Evidence.from_query(f"Lignes présentes en table pour le {d}.", cnt_sql, cnt)]
        if logs.empty:
            h.details["reason"] = "Aucune exécution journalisée pour ce lot."
            return h
        partial = logs[(logs["rows_loaded"] > 0) & (logs["rows_loaded"] < 0.98 * logs["rows_expected"])]
        if partial.empty:
            h.status = REFUTED
            h.details["reason"] = "Toutes les exécutions journalisées ont chargé le volume attendu (ou aucune ligne)."
            return h
        missing = int((partial["rows_expected"] - partial["rows_loaded"]).sum())
        h.status = SUPPORTED
        h.effect = {"orders": -missing, "revenue": round(-missing * self.avg_order_revenue, 2)}
        h.details.update({
            "rows_expected": int(partial["rows_expected"].sum()), "rows_loaded": int(partial["rows_loaded"].sum()),
            "missing_rows": missing, "silent_failure": bool((partial["status"] == "success").all()),
            "revenue_effect_basis": "estimation : lignes manquantes × panier moyen historique",
        })
        h.details["shares"] = {"orders": _share(-missing, gaps["orders"]),
                               "revenue": _share(h.effect["revenue"], gaps["revenue"])}
        h.explained_share = h.details["shares"]["revenue"] if gaps["revenue"] < 0 else h.details["shares"]["orders"]
        return h

    def test_late_load(self, start: date, end: date, gaps: dict | None) -> Hypothesis:
        span = f"du {start}" if start == end else f"du {start} au {end}"
        h = Hypothesis("late_load", "orders", f"Les lots de commandes {span} n'ont pas encore été chargés (retard).",
                       INCONCLUSIVE, ["Lire le statut des exécutions pour chaque date attendue",
                                      "Vérifier l'absence de lignes en table pour ces dates"])
        if not self.has_logs:
            h.status, h.details["reason"] = UNTESTABLE, "Aucun journal de chargement (pipeline_logs) disponible."
            return h
        sql, logs = self._logs(start, end)
        cnt_sql, cnt = self._rows_per_day(start, end)
        h.evidence += [Evidence.from_query(f"Journal de chargement {span}.", sql, logs),
                       Evidence.from_query(f"Lignes présentes en table {span}.", cnt_sql, cnt)]
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        late_days, expected_rows = [], 0
        for day in days:
            day_logs = logs[pd.to_datetime(logs["batch_date"]).dt.date == day]
            loaded = day_logs[(day_logs["rows_loaded"] > 0) & ~day_logs["status"].isin(LATE_STATUSES)]
            if loaded.empty:
                late_days.append(day.isoformat())
                expected_rows += int(day_logs["rows_expected"].max()) if not day_logs.empty and pd.notna(day_logs["rows_expected"].max()) else 0
        if len(late_days) < len(days):
            h.status = REFUTED
            h.details["reason"] = "Au moins un lot de la période a été chargé normalement."
            return h
        h.status = SUPPORTED
        h.effect = {"orders": -expected_rows, "revenue": round(-expected_rows * self.avg_order_revenue, 2)}
        h.details.update({"late_days": late_days, "statuses": sorted(set(logs["status"])) if not logs.empty else ["absent"],
                          "revenue_effect_basis": "estimation : lignes attendues × panier moyen historique"})
        if gaps:
            h.details["shares"] = {"orders": _share(h.effect["orders"], gaps["orders"]),
                                   "revenue": _share(h.effect["revenue"], gaps["revenue"])}
            row = self.daily.loc[pd.Timestamp(start)]
            h.explained_share = 1.0 if row["orders"] == 0 else h.details["shares"]["orders"]
        else:
            h.explained_share = 1.0
        return h

    def test_duplicate_load(self, d: date, gaps: dict) -> Hypothesis:
        h = Hypothesis("duplicate_load", d.isoformat(), f"Des commandes du {d} ont été chargées plusieurs fois.",
                       INCONCLUSIVE, ["Compter les order_id présents plusieurs fois",
                                      "Mesurer le CA avec et sans doublons",
                                      "Compter les exécutions réussies du lot"])
        dup_sql = """
        WITH dup AS (SELECT order_id, count(*) AS n FROM orders WHERE CAST(order_date AS DATE) = ?
                     GROUP BY 1 HAVING count(*) > 1)
        SELECT count(*) AS duplicated_ids, coalesce(sum(n - 1), 0) AS extra_rows FROM dup
        """
        rev_sql = """
        SELECT (SELECT coalesce(sum(amount) FILTER (WHERE status <> 'cancelled'), 0) FROM orders
                WHERE CAST(order_date AS DATE) = ?) AS revenue_as_loaded,
               (SELECT coalesce(sum(amount) FILTER (WHERE status <> 'cancelled'), 0) FROM (
                    SELECT * FROM orders WHERE CAST(order_date AS DATE) = ?
                    QUALIFY row_number() OVER (PARTITION BY order_id ORDER BY order_id) = 1)) AS revenue_deduplicated
        """
        dup = self.wh.query(dup_sql, [d])
        rev = self.wh.query(rev_sql, [d, d])
        h.evidence += [Evidence.from_query(f"Identifiants de commande dupliqués le {d}.", dup_sql, dup),
                       Evidence.from_query(f"CA du {d} avec et sans doublons.", rev_sql, rev)]
        extra = int(dup["extra_rows"].iloc[0])
        if extra == 0:
            h.status = REFUTED
            h.details["reason"] = "Aucun order_id dupliqué ce jour-là."
            return h
        excess = float(rev["revenue_as_loaded"].iloc[0] - rev["revenue_deduplicated"].iloc[0])
        h.status = SUPPORTED
        h.effect = {"orders": extra, "revenue": round(excess, 2)}
        h.details["extra_rows"] = extra
        if self.has_logs:
            sql, logs = self._logs(d, d)
            h.evidence.append(Evidence.from_query(f"Exécutions du lot orders du {d}.", sql, logs))
            runs = logs[logs["status"].str.startswith("success")]
            h.details["successful_runs"] = int(len(runs))
            if len(runs) > 1:
                h.statement = f"Le lot de commandes du {d} a été exécuté {len(runs)} fois sans purge : doublons."
        h.details["shares"] = {"orders": _share(extra, gaps["orders"]), "revenue": _share(excess, gaps["revenue"])}
        h.explained_share = h.details["shares"]["revenue"] if gaps["revenue"] > 0 else h.details["shares"]["orders"]
        return h

    def test_price(self, start: date, end: date, gaps: dict | None, product: str | None = None) -> Hypothesis:
        key = product or "?"
        h = Hypothesis("price_inconsistency", key,
                       f"Des prix unitaires incohérents gonflent le CA du {start}" + ("" if start == end else f" au {end}") + ".",
                       INCONCLUSIVE, ["Comparer chaque prix unitaire à la médiane historique du produit",
                                      "Mesurer le CA excédentaire attribuable aux prix aberrants"])
        df = price_outliers(self.wh, self.ctx, start, end)
        if product:
            df = df[df["product_id"] == product]
        h.evidence.append(Evidence.from_query("Prix unitaires aberrants sur la période.", PRICE_SQL, df))
        if df.empty:
            h.status = REFUTED
            h.details["reason"] = "Aucun prix unitaire aberrant sur la période."
            return h
        excess = float(df["excess_revenue"].sum())
        h.status = SUPPORTED
        h.effect = {"revenue": round(excess, 2)}
        top = df.iloc[0]
        h.cause_key = str(top["product_id"]) if len(df) == 1 or product else ",".join(sorted(df["product_id"]))
        ratio = float(top["ratio"])
        h.details.update({"products": df["product_id"].tolist(), "ratio": round(ratio, 2),
                          "reference_price": round(float(top["reference_price"]), 2),
                          "observed_price": round(float(top["observed_price"]), 2)})
        h.statement = (f"Le prix unitaire de {top['product_id']} est passé de {top['reference_price']:,.2f} € à "
                       f"{top['observed_price']:,.2f} € (×{ratio:.1f}), ce qui gonfle le CA.")
        for power in (10, 100, 1000):
            if abs(ratio / power - 1) < 0.05:
                h.details["pattern"] = f"Facteur ≈ {power} : compatible avec une erreur d'unité (ex. centimes/euros), non prouvée."
        if product and "updated_at" in columns_of(self.wh, "products"):
            cat_sql = "SELECT product_id, price, cost, updated_at FROM products WHERE product_id = ?"
            h.evidence.append(Evidence.from_query("Fiche produit actuelle (référentiel).", cat_sql,
                                                  self.wh.query(cat_sql, [product])))
        if gaps:
            h.details["shares"] = {"revenue": _share(excess, gaps["revenue"])}
            h.explained_share = h.details["shares"]["revenue"]
        else:
            h.explained_share = 1.0
        return h

    def test_missing_categories(self) -> Hypothesis:
        h = Hypothesis("missing_categories", "?", "Une mise à jour du référentiel produits a effacé des catégories.",
                       INCONCLUSIVE, ["Lister les produits sans catégorie et leur date de mise à jour",
                                      "Comparer avec l'instantané précédent du référentiel",
                                      "Lire le journal de chargement du référentiel à ces dates"])
        snap = self.has_snapshot
        sql = ("SELECT p.product_id, p.updated_at, s.category AS snapshot_category, s.snapshot_date "
               "FROM products p LEFT JOIN products_snapshot s USING (product_id) WHERE p.category IS NULL ORDER BY 1"
               if snap else "SELECT product_id, updated_at FROM products WHERE category IS NULL ORDER BY 1")
        df = self.wh.query(sql)
        h.evidence.append(Evidence.from_query("Produits sans catégorie.", sql, df, limit=50))
        if "updated_at" not in df.columns or df["updated_at"].isna().all():
            h.details["reason"] = "Pas de date de mise à jour : impossible de dater la régression."
            return h
        dates = sorted({pd.Timestamp(x).date() for x in df["updated_at"].dropna()})
        h.cause_key = dates[0].isoformat()
        previously_known = int(df["snapshot_category"].notna().sum()) if snap else 0
        warnings = pd.DataFrame()
        if self.has_logs:
            lsql, logs = self._logs(dates[0], dates[-1], table="products")
            h.evidence.append(Evidence.from_query("Journal de chargement du référentiel produits.", lsql, logs))
            warnings = logs[logs["status"] != "success"]
        h.details.update({"products": df["product_id"].tolist(), "updated_on": [d.isoformat() for d in dates],
                          "restorable_from_snapshot": previously_known})
        if previously_known or not warnings.empty:
            h.status = SUPPORTED
            h.statement = (f"Le chargement du référentiel produits du {dates[0]} a laissé {len(df)} produit(s) "
                           f"sans catégorie" + (f" ({previously_known} avaient une catégorie dans l'instantané précédent)"
                                                if previously_known else "") + ".")
            h.explained_share = 1.0
        return h

    def test_integrity(self, anomaly: dict) -> Hypothesis:
        rule_id = anomaly["metric"]
        rule = INTEGRITY_RULES[rule_id]
        where = integrity_where(rule_id, self.ctx.as_of)
        n = int(anomaly["observed"])
        h = Hypothesis("integrity_violation", rule_id,
                       f"{n} ligne(s) de {rule['table']} violent la règle « {rule['label']} ».",
                       SUPPORTED, ["Compter les lignes violant la règle", "Mesurer leur concentration dans le temps"],
                       evidence=[Evidence(**anomaly["evidence"])], explained_share=1.0)
        h.details.update({"rule": rule_id, "label": rule["label"], "table": rule["table"], "violations": n,
                          "columns": [f"{rule['table']}.{c}" for c in rule["columns"]], "where": where,
                          "origin": "non déterminée : les données disponibles ne disent pas quel traitement a produit ces valeurs"})
        date_col = "order_date" if rule["table"] == "orders" else "signup_date" if rule["table"] == "customers" else None
        if date_col and date_col in columns_of(self.wh, rule["table"]) and rule_id != "order_date_missing":
            sql = (f"SELECT date_trunc('month', {date_col}) AS month, count(*) AS violations FROM \"{rule['table']}\" "
                   f"WHERE {where} GROUP BY 1 ORDER BY 2 DESC LIMIT 6")
            dist = self.wh.query(sql)
            h.evidence.append(Evidence.from_query("Répartition mensuelle des violations.", sql, dist))
            if not dist.empty and n:
                top = float(dist["violations"].iloc[0]) / n
                h.details["top_month_share"] = round(top, 4)
                if top >= 0.5:
                    h.details["concentration"] = (f"{top:.0%} des violations sur un seul mois : compatible avec un "
                                                  "traitement ou un lot défaillant (hypothèse non prouvée).")
        return h

    def test_business_variation(self, technical: list[Hypothesis], primary: str, kinds: set[str]) -> Hypothesis:
        h = Hypothesis("business_variation", "-", "Variation métier réelle (promotion, saisonnalité, incident commercial…).",
                       INCONCLUSIVE, ["Vérifier si les causes techniques mesurées expliquent l'écart"])
        shares = [x.explained_share or 0 for x in technical if x.status == SUPPORTED]
        explained = sum(shares)
        if explained >= EXPLAINED_ENOUGH:
            h.status = UNLIKELY
            h.details["reason"] = f"Les causes techniques mesurées expliquent {min(explained, 1):.0%} de l'écart."
        else:
            h.details["reason"] = ("Ne peut être exclue : aucune donnée commerciale (calendrier promotionnel, "
                                   "incidents du site) n'est disponible pour la tester"
                                   + (f" ; les causes techniques n'expliquent que {explained:.0%} de l'écart." if shares else "."))
        return h


def aggregate_root_causes(cases: list[dict]) -> tuple[list[dict], list[dict]]:
    """Fusionne les hypothèses étayées qui désignent la même cause dans plusieurs dossiers."""
    causes: dict[str, dict] = {}
    unexplained = []
    for case in cases:
        supported = [h for h in case["hypotheses"] if h["status"] == SUPPORTED]
        if not supported:
            unexplained.append(case)
        for h in supported:
            key = f"{h['cause_type']}:{h['cause_key']}"
            rc = causes.setdefault(key, {"id": key, "cause_type": h["cause_type"], "cause_key": h["cause_key"],
                                         "statement": h["statement"], "cases": [], "signals": [],
                                         "hypotheses": [], "details": {}})
            rc["cases"].append(case["id"])
            rc["signals"] += [a["id"] for a in case["anomalies"]]
            rc["hypotheses"].append({**h, "case_id": case["id"]})
            if not case["id"].startswith("daily_"):  # le dossier dédié donne l'énoncé le plus précis
                rc["statement"], rc["details"] = h["statement"], h["details"]
            elif not rc["details"]:
                rc["details"] = h["details"]
    return list(causes.values()), unexplained
