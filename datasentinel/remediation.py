"""Proposition de corrections et Agent Vérificateur.

Les corrections sont exécutées uniquement dans un bac à sable en mémoire (copie des tables).
Une cause n'est « confirmée » que si sa correction fait disparaître le signal sur la copie.
Aucune correction n'est appliquée à la source : l'export d'un script approuvé est la seule sortie.
"""
from __future__ import annotations

import re
from datetime import date

import pandas as pd

from .detection import DetectionContext, columns_of, daily_metrics, integrity_where, run_detection, with_baseline
from .models import CONFIRMED, PROBABLE, records
from .warehouse import Sandbox, Warehouse

_FORBIDDEN = re.compile(r"\b(attach|detach|copy|export|import|install|load|pragma|call|read_\w+|glob)\b", re.IGNORECASE)


def _q(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def propose_plan(rc: dict, wh: Warehouse) -> dict:
    ct, key, details = rc["cause_type"], rc["cause_key"], rc.get("details", {})
    plan = {"id": f"plan:{rc['id']}", "root_cause_id": rc["id"], "requires_approval": True,
            "executable_in_sandbox": True, "sql": "", "steps": [], "post_checks": [], "risk": "moyen",
            "rollback": "Sauvegarder la table cible (CREATE TABLE <table>_backup AS SELECT * FROM <table>) "
                        "avant exécution ; restaurer depuis cette sauvegarde en cas d'écart."}
    order_by = "loaded_at" if "loaded_at" in columns_of(wh, "orders") else "order_id"

    if ct == "duplicate_load":
        plan.update(title=f"Dédoublonner les commandes du {key}", risk="faible", sql=(
            f"CREATE OR REPLACE TABLE orders AS SELECT * FROM orders\n"
            f"QUALIFY CAST(order_date AS DATE) <> DATE {_q(key)}\n"
            f"     OR row_number() OVER (PARTITION BY order_id ORDER BY {order_by}) = 1;"),
            steps=["Conserver la première occurrence de chaque order_id du jour concerné",
                   "Ajouter une étape de purge (DELETE par batch_id) avant toute relance de lot"],
            post_checks=["Aucun order_id dupliqué", "Volume du jour revenu dans la plage attendue"])
    elif ct == "price_inconsistency":
        pid = (details.get("products") or [key])[0]
        ref = details.get("reference_price")
        ratio = details.get("ratio", 0)
        factor = next((p for p in (10, 100, 1000) if ratio and abs(ratio / p - 1) < 0.05), None)
        if factor:
            fix_order = f"unit_price = round(unit_price / {factor}, 2), amount = round(quantity * round(unit_price / {factor}, 2), 2)"
            fix_prod = f"price = round(price / {factor}, 2)"
            how = f"Diviser par {factor} les prix aberrants (préserve les remises appliquées)"
        else:
            fix_order = f"unit_price = {ref}, amount = round(quantity * {ref}, 2)"
            fix_prod = f"price = {ref}"
            how = f"Remplacer les prix aberrants par la médiane historique ({ref} €)"
        threshold = f"{ref} * 5"
        plan.update(title=f"Corriger le prix de {pid}", risk="moyen", sql=(
            f"UPDATE orders SET {fix_order}\nWHERE product_id = {_q(pid)} AND unit_price > {threshold};\n"
            f"UPDATE products SET {fix_prod}\nWHERE product_id = {_q(pid)} AND price > {threshold};"),
            steps=[how, "Faire valider le prix de référence par le responsable catalogue",
                   "Ajouter un contrôle de variation de prix (> ×5) à l'ingestion du référentiel"],
            post_checks=["Plus aucun prix aberrant pour ce produit", "Prix catalogue revenu dans la plage historique"])
    elif ct == "missing_categories":
        plan.update(title="Restaurer les catégories depuis l'instantané précédent", risk="faible", sql=(
            "UPDATE products SET category = s.category\nFROM products_snapshot s\n"
            "WHERE products.product_id = s.product_id AND products.category IS NULL AND s.category IS NOT NULL;"),
            steps=["Restaurer les catégories connues dans l'instantané précédent",
                   "Corriger le mapping du flux fournisseur v2 (colonne 'category')",
                   "Faire vérifier les produits restés sans catégorie par le métier"],
            post_checks=["Aucun produit sans catégorie"])
        if "category" not in columns_of(wh, "products_snapshot"):
            plan.update(executable_in_sandbox=False, sql="")
    elif ct == "integrity_violation":
        plan.update(_integrity_plan(details, wh))
    elif ct in ("partial_load", "late_load"):
        days = details.get("late_days") or [key]
        plan.update(
            title=("Recharger le lot partiel du " if ct == "partial_load" else "Relancer les lots en retard : ") + ", ".join(days),
            risk="moyen", executable_in_sandbox=False,
            steps=["Vérifier la disponibilité et l'intégrité du fichier source (nombre de lignes, somme de contrôle)",
                   "Purger les lignes du lot puis relancer l'ingestion (idempotence)",
                   "Afficher un bandeau « période incomplète » sur les tableaux de bord concernés en attendant"],
            post_checks=["rows_loaded = rows_expected dans le journal", "Volume et CA du jour dans la plage attendue"],
            rollback="Le rechargement est idempotent si le lot est purgé par batch_id avant relance.")
        plan["not_executable_reason"] = ("Les lignes manquantes n'existent pas dans l'entrepôt : la correction exige "
                                         "le fichier source. Vérification complète possible uniquement après rechargement.")
    else:
        plan.update(title="Investigation manuelle requise", executable_in_sandbox=False)
    return plan


def _integrity_plan(d: dict, wh: Warehouse) -> dict:
    """Corrections reprises des règles métier de la base source (table DQ_Corrections)."""
    rule, where = d["rule"], d["where"]
    cols = columns_of(wh, "orders")
    has_margin = {"margin", "cost"} <= cols
    base = {"title": f"Corriger : {d['label'].lower()}", "risk": "moyen",
            "post_checks": [f"Plus aucune ligne ne viole la règle « {d['label']} »"]}
    if rule == "amount_missing" and "price" in columns_of(wh, "products"):
        margin = ", margin = round(orders.quantity * p.price, 2) - orders.cost" if has_margin else ""
        return {**base, "sql": (f"UPDATE orders SET amount = round(orders.quantity * p.price, 2), unit_price = p.price{margin}\n"
                                f"FROM products p WHERE orders.product_id = p.product_id AND orders.amount IS NULL;"),
                "steps": ["Reconstituer le CA selon la règle métier : CA = Prix_Unitaire × Quantité",
                          "Signaler ces lignes comme « valeur reconstituée » dans le tableau de bord",
                          "Identifier le traitement amont qui produit des CA vides"]}
    if rule == "quantity_non_positive":
        return {**base, "sql": ("UPDATE orders SET quantity = abs(quantity),\n"
                                "       unit_price = CASE WHEN quantity <> 0 THEN amount / abs(quantity) END\n"
                                "WHERE quantity < 0;"),
                "post_checks": ["Plus aucune quantité négative (les quantités nulles demandent une revue métier)"],
                "steps": ["Convertir les quantités négatives en valeur absolue (méthode de correction existante)",
                          "Vérifier avec le métier qu'il ne s'agit pas de retours légitimes"]}
    if rule == "cost_negative":
        margin = ", margin = amount - abs(cost)" if has_margin else ""
        return {**base, "sql": f"UPDATE orders SET cost = abs(cost){margin}\nWHERE cost < 0;",
                "steps": ["Convertir les coûts négatifs en valeur absolue (méthode de correction existante)",
                          "Recalculer la marge correspondante"]}
    if rule == "margin_inconsistent":
        return {**base, "risk": "faible", "sql": f"UPDATE orders SET margin = amount - cost\nWHERE {where};",
                "steps": ["Recalculer la marge selon la règle métier : Marge = CA − Coût"]}
    steps = {
        "amount_negative": ["Faire qualifier ces lignes par le métier (avoirs, retours ou erreurs de signe ?)"],
        "order_date_missing": ["Retrouver la date de vente dans le système source",
                               "Compléter Dim_Date ou corriger la clé de date"],
        "product_orphan": ["Ajouter les produits manquants au référentiel ou corriger la clé produit"],
        "customer_orphan": ["Ajouter les clients manquants au référentiel ou corriger la clé client"],
        "signup_in_future": ["Vérifier dans le CRM la vraie date d'inscription de ces clients",
                             "Ajouter un contrôle « date ≤ date de chargement » à l'alimentation de Dim_Client",
                             "Exclure ces clients des analyses de cohortes en attendant"],
    }.get(rule, ["Investigation manuelle"])
    return {**base, "executable_in_sandbox": False, "sql": "", "steps": steps,
            "not_executable_reason": "Aucune valeur de remplacement ne peut être déduite des données : "
                                     "décision métier ou donnée source requise."}


def validate_sandbox_sql(sql: str) -> None:
    if _FORBIDDEN.search(sql):
        raise PermissionError("Instruction interdite dans le bac à sable (accès fichiers/extensions).")


class Verifier:
    """Teste chaque correction sur une copie isolée et classe les causes."""

    def __init__(self, wh: Warehouse, ctx: DetectionContext):
        self.wh, self.ctx = wh, ctx

    def _apply(self, sb: Sandbox, plan: dict) -> None:
        validate_sandbox_sql(plan["sql"])
        for stmt in [s.strip() for s in plan["sql"].split(";") if s.strip()]:
            sb.execute(stmt)

    def _check(self, sb: Sandbox, name: str, sql: str, params: list, ok) -> dict:
        df = sb.query(sql, params)
        value = df.iloc[0, 0] if not df.empty else None
        value = value.item() if hasattr(value, "item") else value
        return {"name": name, "passed": bool(ok(value)), "observed": value, "sql": " ".join(sql.split()),
                "result": records(df, 5)}

    def checks_for(self, sb: Sandbox, rc: dict) -> list[dict]:
        ct, key, details = rc["cause_type"], rc["cause_key"], rc.get("details", {})
        tol = self.ctx.settings.min_relative_deviation
        if ct == "duplicate_load":
            d = date.fromisoformat(key)
            daily = with_baseline(daily_metrics(sb, self.ctx.history_start, self.ctx.as_of))
            dev = float(daily.loc[pd.Timestamp(d), "orders_dev"])
            return [
                self._check(sb, "Aucun order_id dupliqué ce jour-là",
                            "SELECT count(*) - count(DISTINCT order_id) AS duplicate_rows FROM orders WHERE CAST(order_date AS DATE) = ?",
                            [d], lambda v: v == 0),
                {"name": f"Volume du jour dans la plage attendue (±{tol:.0%})", "passed": abs(dev) < tol,
                 "observed": round(dev, 4), "sql": "daily_metrics + médiane 4 semaines", "result": []},
            ]
        if ct == "price_inconsistency":
            pid = (details.get("products") or [key])[0]
            k = self.ctx.settings.price_outlier_ratio
            return [self._check(
                sb, "Plus aucun prix aberrant pour le produit",
                f"""WITH ref AS (SELECT median(unit_price) AS r FROM orders WHERE product_id = ? AND CAST(order_date AS DATE) < ?)
                SELECT count(*) AS outliers FROM orders, ref WHERE product_id = ?
                  AND (unit_price > ref.r * {k} OR unit_price < ref.r / {k})""",
                [pid, self.ctx.window_start, pid], lambda v: v == 0),
                self._check(sb, "Prix catalogue cohérent avec l'historique",
                            "SELECT price / ? AS ratio FROM products WHERE product_id = ?",
                            [details.get("reference_price") or 1, pid], lambda v: v is not None and 0.5 <= v <= 2)]
        if ct == "integrity_violation":
            where = integrity_where(details["rule"], self.ctx.as_of)
            if details["rule"] == "quantity_non_positive":
                where = "quantity < 0"
            return [self._check(sb, f"Plus aucune ligne « {details['label']} »",
                                f'SELECT count(*) AS n FROM "{details["table"]}" WHERE {where}', [], lambda v: v == 0)]
        if ct == "missing_categories":
            return [self._check(sb, "Aucun produit sans catégorie",
                                "SELECT count(*) AS n FROM products WHERE category IS NULL", [], lambda v: v == 0)]
        return []

    def test_plan(self, rc: dict, plan: dict) -> dict:
        if not plan.get("executable_in_sandbox") or not plan.get("sql"):
            return {"plan_id": plan["id"], "executed": False, "checks": [], "passed": None,
                    "reason": plan.get("not_executable_reason", "Correction non exécutable sur copie.")}
        sb = self.wh.sandbox()
        try:
            self._apply(sb, plan)
            checks = self.checks_for(sb, rc)
        except Exception as exc:
            return {"plan_id": plan["id"], "executed": False, "checks": [], "passed": False, "reason": f"Échec : {exc}"}
        finally:
            sb.close()
        return {"plan_id": plan["id"], "executed": True, "checks": checks,
                "passed": bool(checks) and all(c["passed"] for c in checks)}

    def residual_after_all(self, plans: list[dict]) -> dict:
        """Applique toutes les corrections exécutables ensemble puis relance la détection complète."""
        sb = self.wh.sandbox()
        try:
            applied = []
            for plan in plans:
                if plan.get("executable_in_sandbox") and plan.get("sql") and plan.get("decision") != "rejected":
                    self._apply(sb, plan)
                    applied.append(plan["id"])
            det = run_detection(sb, self.ctx.settings, self.ctx.as_of)
        finally:
            sb.close()
        return {"applied_plans": applied, "residual_anomalies": [
            {k: a[k] for k in ("id", "kind", "summary")} for a in det["anomalies"]]}

    @staticmethod
    def classify(rc: dict, verification: dict, cases: list[dict]) -> dict:
        """Statut et confiance déterminés par des règles explicites, jamais par un LLM."""
        hyps = rc["hypotheses"]
        shares = [h["explained_share"] for h in hyps if h.get("explained_share") is not None]
        best = max(shares) if shares else None
        justification = [f"{len(rc['signals'])} signal(aux) observé(s) relié(s) à cette cause : {', '.join(rc['signals'][:6])}"
                         + ("…" if len(rc["signals"]) > 6 else "")]
        justification += [f"Test « {t} » exécuté" for t in hyps[0]["tests"]]
        if best is not None:
            justification.append(f"Part de l'écart expliquée par cette cause (meilleur dossier) : {best:.0%}")
        uncertainties = []
        for case in cases:
            if case["id"] in rc["cases"] and case["residual_share"] > 0.2:
                uncertainties.append(f"Dossier {case['id']} : {case['residual_share']:.0%} de l'écart reste inexpliqué.")
            for h in case["hypotheses"]:
                if case["id"] in rc["cases"] and h["cause_type"] == "business_variation" and h["status"] != "unlikely":
                    uncertainties.append(f"Dossier {case['id']} : variation métier non exclue — {h['details'].get('reason', '')}")
        if rc["cause_type"] == "integrity_violation":
            uncertainties.append("Le constat est établi ligne à ligne, mais son origine amont n'est "
                                 "pas déterminée par les données disponibles.")
            if rc["details"].get("rule") == "amount_missing":
                uncertainties.append("Le CA corrigé est reconstitué (Prix_Unitaire × Quantité), pas lu dans la source.")
            if rc["details"].get("concentration"):
                uncertainties.append(rc["details"]["concentration"])
        estimated = any("estimation" in str(h["details"].get("revenue_effect_basis", "")) for h in hyps)
        if estimated:
            uncertainties.append("L'effet sur le CA est estimé (lignes manquantes × panier moyen), pas mesuré.")

        if verification.get("passed"):
            status, confidence = CONFIRMED, "élevée"
            justification.append("Correction testée sur une copie : tous les contrôles de vérification passent.")
        else:
            status = PROBABLE
            if verification.get("executed") and verification.get("passed") is False:
                uncertainties.append("La correction testée sur copie ne fait pas disparaître entièrement le signal.")
            elif not verification.get("executed"):
                uncertainties.append(verification.get("reason", "Correction non vérifiable sur copie."))
            confidence = "élevée" if best is not None and best >= 0.8 else "moyenne" if best and best >= 0.4 else "faible"
        return {"status": status, "confidence": confidence, "justification": justification,
                "uncertainties": list(dict.fromkeys(uncertainties))}
