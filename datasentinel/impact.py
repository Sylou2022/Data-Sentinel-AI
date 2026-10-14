"""Agent Impact : relie une cause aux KPI, tableaux de bord et décisions qui en dépendent.

Le lignage est *déclaré* (catalogue ci-dessous) : l'application n'invente pas de dépendances.
Les écarts sont mesurés quand une version corrigée est calculable, sinon estimés et signalés comme tels.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from .detection import DetectionContext, columns_of
from .warehouse import Warehouse

KPI_CATALOG: dict[str, dict] = {
    "revenue": {
        "label": "Chiffre d'affaires", "lineage": ["orders.amount", "orders.status", "orders.order_date"],
        "dashboards": ["Ventes quotidiennes", "Comité de direction — CA mensuel"],
        "decisions": ["Suivi des objectifs commerciaux", "Prévisions de trésorerie"]},
    "order_count": {
        "label": "Nombre de commandes", "lineage": ["orders.order_id", "orders.order_date"],
        "dashboards": ["Ventes quotidiennes", "Opérations logistiques"],
        "decisions": ["Dimensionnement des équipes logistiques"]},
    "average_basket": {
        "label": "Panier moyen", "lineage": ["orders.amount", "orders.order_id", "orders.status"],
        "dashboards": ["Performance marketing"], "decisions": ["Évaluation des campagnes marketing"]},
    "gross_margin": {
        "label": "Marge brute", "lineage": ["orders.amount", "orders.quantity", "orders.cost", "orders.margin",
                                            "orders.product_id", "products.cost"],
        "dashboards": ["Rentabilité produits"], "decisions": ["Politique tarifaire", "Arbitrages promotionnels"]},
    "revenue_by_category": {
        "label": "CA par catégorie", "lineage": ["orders.amount", "orders.product_id", "products.category"],
        "dashboards": ["Performance par catégorie"], "decisions": ["Arbitrages d'assortiment", "Budgets d'achat par catégorie"]},
    "revenue_by_region": {
        "label": "CA par région", "lineage": ["orders.amount", "orders.region_id", "customers.region"],
        "dashboards": ["Performance régionale"], "decisions": ["Objectifs des équipes régionales"]},
    "customer_acquisition": {
        "label": "Nouveaux clients", "lineage": ["customers.signup_date", "orders.customer_id"],
        "dashboards": ["Acquisition clients"], "decisions": ["Budget d'acquisition", "Analyse de cohortes et fidélisation"]},
}

# Colonnes touchées par chaque type de cause ("orders.*" = des lignes entières manquent ou sont en trop)
CAUSE_FOOTPRINT = {
    "partial_load": ["orders.*"],
    "late_load": ["orders.*"],
    "duplicate_load": ["orders.*"],
    "price_inconsistency": ["orders.unit_price", "orders.amount"],
    "missing_categories": ["products.category"],
}

KPI_SQL = """
WITH o AS ({relation})
SELECT count(*) AS order_count,
       coalesce(sum(o.amount) FILTER (WHERE o.status <> 'cancelled'), 0) AS revenue,
       coalesce(sum(o.amount) FILTER (WHERE o.status <> 'cancelled'), 0)
           / nullif(count(*) FILTER (WHERE o.status <> 'cancelled'), 0) AS average_basket,
       coalesce(sum({margin}) FILTER (WHERE o.status <> 'cancelled'), 0) AS gross_margin
FROM o LEFT JOIN products p USING (product_id)
"""


def affected_kpis(cause_type: str, footprint: list[str] | None = None) -> list[str]:
    footprint = footprint or CAUSE_FOOTPRINT.get(cause_type, [])
    out = []
    for kpi, spec in KPI_CATALOG.items():
        for col in spec["lineage"]:
            if col in footprint or any(f.endswith(".*") and col.startswith(f[:-1]) for f in footprint):
                out.append(kpi)
                break
    return out


class ImpactAnalyst:
    def __init__(self, wh: Warehouse, ctx: DetectionContext, avg_order_revenue: float, daily: pd.DataFrame):
        self.wh, self.ctx, self.avg_order_revenue, self.daily = wh, ctx, avg_order_revenue, daily
        order_cols = columns_of(wh, "orders")
        # Marge : colonne source si elle existe, sinon CA − coût (ligne puis référentiel produit).
        self.margin_expr = ("o.margin" if "margin" in order_cols else "o.amount - o.cost" if "cost" in order_cols
                            else "o.amount - o.quantity * p.cost" if "cost" in columns_of(wh, "products") else "NULL")
        month_start = ctx.as_of.replace(day=1)
        mtd = self._kpis("SELECT * FROM orders WHERE CAST(order_date AS DATE) BETWEEN ? AND ?", [month_start, ctx.as_of])
        self.month_start, self.mtd_revenue = month_start, float(mtd["revenue"].iloc[0])

    def _kpis(self, relation: str, params: list, wh: Warehouse | None = None) -> pd.DataFrame:
        return (wh or self.wh).query(KPI_SQL.format(relation=relation, margin=self.margin_expr), params)

    def _rows(self, kpis: list[str], before: pd.Series, after: pd.Series, period: str, basis: str) -> list[dict]:
        rows = []
        for kpi in kpis:
            if kpi not in before.index or pd.isna(before[kpi]):
                continue
            b, a = float(before[kpi]), float(after[kpi]) if pd.notna(after[kpi]) else None
            rows.append({"kpi": kpi, "label": KPI_CATALOG[kpi]["label"], "period": period,
                         "as_reported": round(b, 2), "reference": None if a is None else round(a, 2),
                         "delta": None if a is None else round(b - a, 2),
                         "delta_pct": None if not a else round((b - a) / a, 4), "basis": basis})
        return rows

    def _integrity(self, rc: dict, kpis: list[str]) -> tuple[list[dict], list[str]]:
        from .remediation import propose_plan

        d = rc["details"]
        notes = [f"Origine {d['origin']}."]
        measured: list[dict] = []
        if d["table"] == "orders":
            sql = f"SELECT count(*) AS n, coalesce(sum(amount), 0) AS amount FROM orders WHERE {d['where']}"
            row = self.wh.query(sql).iloc[0]
            measured.append({"kpi": "revenue", "label": "Montant des lignes concernées", "period": "toute la table",
                             "as_reported": round(float(row["amount"]), 2), "reference": None, "delta": None,
                             "delta_pct": None, "basis": f"mesuré ({int(row['n'])} lignes)"})
        elif d["table"] == "customers" and "customer_id" in columns_of(self.wh, "orders"):
            sql = (f"SELECT count(*) AS n, count(DISTINCT o.customer_id) AS customers, coalesce(sum(o.amount), 0) AS amount "
                   f"FROM orders o WHERE o.customer_id IN (SELECT customer_id FROM customers WHERE {d['where']})")
            row = self.wh.query(sql).iloc[0]
            measured.append({"kpi": "customer_acquisition", "label": "CA des clients concernés", "period": "toute la table",
                             "as_reported": round(float(row["amount"]), 2), "reference": None, "delta": None,
                             "delta_pct": None,
                             "basis": f"mesuré ({int(row['n'])} ventes de {int(row['customers'])} clients, "
                                      "attribuées à des cohortes erronées)"})
        plan = propose_plan(rc, self.wh)
        order_kpis = [k for k in kpis if k in ("revenue", "order_count", "average_basket", "gross_margin")]
        if plan["executable_in_sandbox"] and order_kpis:
            from .remediation import validate_sandbox_sql

            validate_sandbox_sql(plan["sql"])
            sb = self.wh.sandbox()
            try:
                for stmt in [x.strip() for x in plan["sql"].split(";") if x.strip()]:
                    sb.execute(stmt)
                before = self._kpis("SELECT * FROM orders", []).iloc[0]
                after = self._kpis("SELECT * FROM orders", [], wh=sb).iloc[0]
            finally:
                sb.close()
            measured += [m for m in self._rows(order_kpis, before, after, "toute la table",
                                               "mesuré (avant / après la correction proposée, sur copie)")
                         if m["delta"]]
        elif not plan["executable_in_sandbox"]:
            notes.append("Écart sur les KPI non quantifiable sans décision métier ou données sources complémentaires.")
        return measured, notes

    def assess(self, rc: dict) -> dict:
        ct, key, details = rc["cause_type"], rc["cause_key"], rc.get("details", {})
        kpis = affected_kpis(ct, details.get("columns") if ct == "integrity_violation" else None)
        measured: list[dict] = []
        notes: list[str] = []

        if ct == "duplicate_load":
            d = date.fromisoformat(key)
            before = self._kpis("SELECT * FROM orders WHERE CAST(order_date AS DATE) = ?", [d]).iloc[0]
            after = self._kpis("SELECT * FROM orders WHERE CAST(order_date AS DATE) = ? "
                               "QUALIFY row_number() OVER (PARTITION BY order_id ORDER BY order_id) = 1", [d]).iloc[0]
            measured = self._rows(kpis, before, after, key, "mesuré (recalcul sans doublons)")
        elif ct == "price_inconsistency":
            products = details.get("products") or [key]
            relation = """
            SELECT o.* REPLACE (CASE WHEN r.ref_price IS NOT NULL AND (o.unit_price > r.ref_price * {k} OR o.unit_price < r.ref_price / {k})
                                     THEN round(o.quantity * r.ref_price, 2) ELSE o.amount END AS amount)
            FROM orders o LEFT JOIN (SELECT product_id, median(unit_price) AS ref_price FROM orders
                                     WHERE CAST(order_date AS DATE) < ? GROUP BY 1) r USING (product_id)
            WHERE CAST(o.order_date AS DATE) BETWEEN ? AND ?
            """.format(k=self.ctx.settings.price_outlier_ratio)
            start = self.ctx.window_start
            before = self._kpis("SELECT * FROM orders WHERE CAST(order_date AS DATE) BETWEEN ? AND ?", [start, self.ctx.as_of]).iloc[0]
            after = self._kpis(relation, [start, start, self.ctx.as_of]).iloc[0]
            measured = self._rows(kpis, before, after, f"{start} → {self.ctx.as_of}",
                                  "mesuré (prix remplacé par la médiane historique)")
            notes.append(f"Produits concernés : {', '.join(products)}.")
        elif ct in ("partial_load", "late_load"):
            days = details.get("late_days") or [key]
            obs = self.daily.loc[[pd.Timestamp(x) for x in days]]
            before = pd.Series({"order_count": obs["orders"].sum(), "revenue": obs["revenue"].sum(), "average_basket": None,
                                "gross_margin": None})
            after = pd.Series({"order_count": obs["orders_expected"].sum(), "revenue": obs["revenue_expected"].sum(),
                               "average_basket": None, "gross_margin": None})
            measured = self._rows(["order_count", "revenue"], before, after, " , ".join(days),
                                  "estimé (référence = médiane des mêmes jours de semaine)")
            notes.append("Marge, panier moyen et ventilations sont aussi affectés mais ne peuvent être quantifiés "
                         "qu'après rechargement des lignes manquantes.")
        elif ct == "integrity_violation":
            measured, notes = self._integrity(rc, kpis)
        elif ct == "missing_categories":
            sql = """
            SELECT coalesce(s.category, 'inconnue') AS former_category, count(DISTINCT o.product_id) AS products,
                   sum(o.amount) FILTER (WHERE o.status <> 'cancelled') AS revenue_without_category
            FROM orders o JOIN products p USING (product_id)
            LEFT JOIN products_snapshot s USING (product_id)
            WHERE p.category IS NULL AND CAST(o.order_date AS DATE) BETWEEN ? AND ?
            GROUP BY 1 ORDER BY 3 DESC
            """
            if "category" not in columns_of(self.wh, "products_snapshot"):
                sql = sql.replace("coalesce(s.category, 'inconnue')", "'inconnue'").replace(
                    "LEFT JOIN products_snapshot s USING (product_id)", "")
            df = self.wh.query(sql, [self.ctx.window_start, self.ctx.as_of])
            total = float(df["revenue_without_category"].sum())
            measured = [{"kpi": "revenue_by_category", "label": KPI_CATALOG["revenue_by_category"]["label"],
                         "period": f"{self.ctx.window_start} → {self.ctx.as_of}", "as_reported": None, "reference": None,
                         "delta": round(-total, 2), "delta_pct": None,
                         "basis": "mesuré (CA rattaché à aucune catégorie)",
                         "breakdown": [{k: (v.item() if hasattr(v, "item") else v) for k, v in r.items()}
                                       for r in df.to_dict(orient="records")]}]

        revenue_delta = next((m["delta"] for m in measured if m["kpi"] == "revenue" and m["delta"] is not None), None)
        dashboards = sorted({d for k in kpis for d in KPI_CATALOG[k]["dashboards"]})
        decisions = sorted({d for k in kpis for d in KPI_CATALOG[k]["decisions"]})
        return {
            "affected_kpis": [{"kpi": k, "label": KPI_CATALOG[k]["label"]} for k in kpis],
            "measurements": measured,
            "dashboards": dashboards,
            "decisions": decisions,
            "month_to_date": {"from": self.month_start.isoformat(), "to": self.ctx.as_of.isoformat(),
                              "revenue_as_reported": round(self.mtd_revenue, 2),
                              "revenue_error_share": round(revenue_delta / self.mtd_revenue, 4)
                              if revenue_delta is not None and self.mtd_revenue else None},
            "lineage_source": "catalogue KPI déclaré (datasentinel/impact.py) — pas de lignage automatique",
            "notes": notes,
        }
