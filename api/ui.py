"""Interface web : modèle de vue et actions de l'analyste.

Transforme l'état LangGraph d'une enquête en données prêtes à afficher par `web/app.js`
(Vue d'ensemble, Import, Enquête, Rapport). Aucune valeur n'est inventée : tout provient
de la détection, des tests et du journal d'audit. Les niveaux de preuve restent stricts :
Observée (fait mesuré), Hypothèse (étayée mais non vérifiée), Confirmée (correction vérifiée sur copie).
"""
from __future__ import annotations

import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from datasentinel.connectors import snapshot_metadata
from datasentinel.detection import CHECKS, daily_metrics, with_baseline
from datasentinel.evaluation import evaluate
from datasentinel.generator import SCENARIOS
from datasentinel.llm import fmt_eur
from datasentinel.models import CONFIRMED, STATUS_LABELS, records
from datasentinel.service import DataSentinel
from datasentinel.warehouse import Warehouse

from .deps import get_service

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
router = APIRouter(prefix="/ui/api", include_in_schema=False)

SEVERITIES = {"critical": "Critique", "high": "Haute", "medium": "Moyenne", "low": "Basse"}
SEVERITY_ORDER = list(SEVERITIES)
# Ordre de choix du signal qui donne son titre à un incident (du plus spécifique au plus général).
HEADLINE_PRIORITY = ["duplicate_keys", "price_outlier", "freshness", "missing_category", "integrity",
                     "revenue_drop", "revenue_spike", "volume_drop", "volume_spike"]
TITLES = {"duplicate_keys": "Doublons de commandes", "price_outlier": "Prix produit incohérent",
          "freshness": "Chargement retardé", "missing_category": "Catégories manquantes"}
SUBTITLES = {"duplicate_keys": "Ventes possiblement surévaluées",
             "price_outlier": "Variation anormale du chiffre d'affaires et de la marge",
             "freshness": "Indicateurs incomplets sur la période courante",
             "missing_category": "Analyse produit partielle",
             "integrity": "Règle d'intégrité non respectée",
             "volume_drop": "Commandes possiblement manquantes",
             "volume_spike": "Commandes possiblement en excès"}
AGENTS = [("Détective", ["Agent Détective"], "Recherche les écarts inhabituels dans les données."),
          ("Enquêteur", ["Agent Enquêteur"], "Examine dates de chargement, volumes et tables sources."),
          ("Impact", ["Agent Impact"], "Estime les KPI affectés à partir des dépendances disponibles."),
          ("Vérificateur", ["Agent Correcteur", "Agent Vérificateur"],
           "Teste les hypothèses, sépare faits et causes incertaines.")]
HYP_ORDER = {"supported": 0, "inconclusive": 1, "untestable": 2, "unlikely": 3, "refuted": 4}
HYP_BADGES = {"inconclusive": "Incertain", "untestable": "Non testée", "unlikely": "Peu probable", "refuted": "Réfutée"}
_SQL_BREAKS = re.compile(r"\s+(FROM|WHERE|GROUP BY|ORDER BY|HAVING|LEFT JOIN|INNER JOIN|JOIN|UNION ALL|UNION|QUALIFY|LIMIT)\b")

LAB_NAME = "Meridian Retail"
# Enquêtes lancées en arrière-plan : run_id -> {"done", "error", "warning", "db_path"}
_RUNS: dict[str, dict] = {}
_RUNS_LOCK = threading.RLock()  # réentrant : default_source appelle launch_run sous verrou
# Analyse initiale de la base SQL Server reliée (lancée automatiquement quand aucune source n'est choisie)
_DEFAULT: dict[str, str | None] = {"run_id": None}


# ---------------------------------------------------------------- utilitaires
def local(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone()


def hhmm(ts: str) -> str:
    return local(ts).strftime("%H:%M")


def day_label(ts: str) -> str:
    d = local(ts).date()
    return f"aujourd'hui, {d:%d/%m}" if d == date.today() else f"le {d:%d/%m}"


def ddmm(iso: str) -> str:
    return date.fromisoformat(iso[:10]).strftime("%d/%m")


def lc_first(text: str) -> str:
    text = text.rstrip(". ")
    return text[:1].lower() + text[1:] if text[1:2].islower() or not text[1:2] else text


def pct(x: float) -> str:
    return f"{round(abs(x) * 100):d} %"


def fmt_sql(sql: str) -> str:
    return "\n".join(line.rstrip() for line in _SQL_BREAKS.sub(r"\n\1", sql.strip()).splitlines())


def source_kind(db_path: str | None) -> str | None:
    if not db_path:
        return None
    if snapshot_metadata(db_path):
        return "sqlserver"
    return "lab" if "labs" in Path(db_path).parts else "csv"


def _stamp(extracted_at: str) -> str:
    try:
        return f"{datetime.strptime(extracted_at, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).astimezone():%d/%m à %H:%M}"
    except ValueError:
        return extracted_at


def source_label(db_path: str | None, svc: DataSentinel | None = None) -> str:
    if not db_path:
        db = svc.settings.sqlserver.database if svc else "SQL Server"
        return f"Source active : SQL Server {db} (extraction en cours)"
    meta = snapshot_metadata(db_path)
    if meta:
        return f"Source active : SQL Server {meta['database']} · instantané du {_stamp(meta['extracted_at'])}"
    if "labs" in Path(db_path).parts:
        return f"Source active : {LAB_NAME} (données synthétiques)"
    return "Source active : fichiers CSV importés"


# ---------------------------------------------------------------- incidents
def _headline(anoms: list[dict]) -> dict:
    def rank(a):
        return (HEADLINE_PRIORITY.index(a["kind"]) if a["kind"] in HEADLINE_PRIORITY else len(HEADLINE_PRIORITY),
                -abs(a.get("deviation") or 0))
    return sorted(anoms, key=rank)[0]


def _title(a: dict) -> str:
    kind = a["kind"]
    if kind.startswith(("revenue_", "volume_")):
        metric = "Chiffre d'affaires" if kind.startswith("revenue_") else "Volume de commandes"
        direction = "baisse" if kind.endswith("_drop") else "hausse"
        return f"{metric} en {direction} de {pct(a['deviation'])}" if a.get("deviation") is not None else metric
    if kind == "integrity":
        return a["summary"].split(" : ")[0]
    return TITLES.get(kind, a["summary"])


def _subtitle(a: dict, kinds: set[str]) -> str:
    kind = a["kind"]
    if kind == "revenue_drop":
        return "Volume de commandes anormalement bas" if "volume_drop" in kinds else "Montants anormalement bas"
    if kind == "revenue_spike":
        return "Volume de commandes anormalement haut" if "volume_spike" in kinds else "Montants anormalement hauts"
    return SUBTITLES.get(kind, a["summary"])


def _severity(anoms: list[dict]) -> str:
    """Règle explicite : critique si un écart fort touche le CA, haute pour tout autre écart fort,
    moyenne pour les écarts modérés sur les commandes, basse pour les référentiels."""
    high = [a for a in anoms if a["severity"] == "high"]
    if any(a["kind"].startswith("revenue_") for a in high):
        return "critical"
    if high:
        return "high"
    return "medium" if any(a["table"] == "orders" for a in anoms) else "low"


def _period(anoms: list[dict]) -> tuple[str, str]:
    return min(a["period_start"][:10] for a in anoms), max(a["period_end"][:10] for a in anoms)


def _unexplained_groups(cases: list[dict]) -> list[tuple[list[dict], list[str]]]:
    """Regroupe les dossiers sans cause qui décrivent le même fait observé :
    - les jours en baisse compris dans la période d'un retard de fraîcheur rejoignent ce retard ;
    - les jours consécutifs présentant les mêmes signaux forment un seul incident."""
    daily = sorted((c for c in cases if c["id"].startswith("daily_")), key=lambda c: c["id"])
    other = [c for c in cases if not c["id"].startswith("daily_")]
    out = []
    for fresh in [c for c in other if c["anomalies"][0]["kind"] == "freshness"]:
        a = fresh["anomalies"][0]
        inside = [c for c in daily if a["period_start"][:10] <= c["id"][6:] <= a["period_end"][:10]
                  and all(x["kind"].endswith("_drop") for x in c["anomalies"])]
        daily = [c for c in daily if c not in inside]
        out.append((fresh["anomalies"] + [x for c in inside for x in c["anomalies"]], [fresh["id"]] + [c["id"] for c in inside]))
    out += [(c["anomalies"], [c["id"]]) for c in other if c["anomalies"][0]["kind"] != "freshness"]
    run: list[dict] = []
    for c in daily + [None]:
        prev = run[-1] if run else None
        if c and prev and date.fromisoformat(c["id"][6:]) - date.fromisoformat(prev["id"][6:]) == timedelta(days=1) \
                and {x["kind"] for x in c["anomalies"]} == {x["kind"] for x in prev["anomalies"]}:
            run.append(c)
            continue
        if run:
            out.append(([x for r in run for x in r["anomalies"]], [r["id"] for r in run]))
        run = [c] if c else []
    return out


def incidents(state: dict) -> list[dict]:
    """Regroupe les anomalies par cause étayée (ou par dossier sans cause)."""
    anomalies = {a["id"]: a for a in state.get("detection", {}).get("anomalies", [])}
    plans = {p["root_cause_id"]: p for p in state.get("plans", [])}
    out = []
    groups = [(rc, [anomalies[s] for s in dict.fromkeys(rc["signals"]) if s in anomalies], rc["cases"])
              for rc in state.get("root_causes", [])]
    groups += [(None, anoms, case_ids) for anoms, case_ids in _unexplained_groups(state.get("unexplained", []))]
    for rc, anoms, case_ids in groups:
        if not anoms:
            continue
        head = _headline(anoms)
        status = (rc or {}).get("assessment", {}).get("status")
        plan = plans.get(rc["id"]) if rc else None
        start, end = _period(anoms)
        out.append({
            "id": rc["id"] if rc else f"case:{case_ids[0]}", "rc": rc, "plan": plan, "anomalies": anoms,
            "case_ids": case_ids, "headline": head, "title": _title(head),
            "subtitle": f"{_subtitle(head, {a['kind'] for a in anoms})} · {ddmm(start)}"
                        + (f" au {ddmm(end)}" if end != start else ""),
            "table": head["table"], "severity": _severity(anoms), "period": (start, end),
            "level": "observed" if not rc else ("confirmed" if status == CONFIRMED else "hypothesis"),
            "resolved": bool(plan) and plan.get("decision") == "approved",
        })
    return sorted(out, key=lambda i: (SEVERITY_ORDER.index(i["severity"]), i["period"][0]))


def run_label(state: dict | None, paused: bool, running: bool) -> str:
    if running or state is None:
        return "enquête en cours"
    if paused:
        return "enquête en pause"
    return {"awaiting_review": "en attente de validation", "completed": "enquête terminée"}.get(state["status"],
                                                                                                "enquête en cours")


def agents_view(state: dict | None, running: bool, error: str | None) -> list[dict]:
    trace = (state or {}).get("trace", [])
    done_names = {t["agent"] for t in trace}
    finished = state is not None and not running
    out, active_found = [], False
    for i, (name, members, generic) in enumerate(AGENTS, 1):
        steps = [t for t in trace if t["agent"] in members]
        done = members[-1] in done_names
        if done:
            status, desc = "done", steps[-1]["summary"]
        elif error and not active_found:
            status, desc, active_found = "error", f"Échec : {error}", True
        elif running and not active_found:
            status, desc, active_found = "running", generic, True
        elif finished:
            status, desc = "skipped", "Non requis : aucune anomalie à examiner."
        else:
            status, desc = "pending", generic
        out.append({"n": i, "name": name, "status": status, "description": desc})
    return out


def overview_kpis(svc: DataSentinel, state: dict | None, incs: list[dict], runs: list[dict]) -> list[dict]:
    open_incs = [i for i in incs if not i["resolved"]]
    critical = sum(i["severity"] == "critical" for i in open_incs)
    in_progress, active = 0, 0
    for r in runs[:30]:
        info = _RUNS.get(r["run_id"])
        if info and not info["done"]:
            in_progress, active = in_progress + 1, active + 1
            continue
        try:
            in_progress += svc.get(r["run_id"])["status"] != "completed"
        except KeyError:
            continue
    pending = [p for p in (state or {}).get("plans", []) if p.get("decision") not in ("approved", "rejected")] \
        if state and state.get("awaiting_review") else []
    last = runs[0]["ts"] if runs else None
    return [
        {"label": "Anomalies ouvertes", "value": str(len(open_incs)),
         "sub": f"dont {critical} critique" + ("s" if critical > 1 else "")},
        {"label": "Enquêtes en cours", "value": str(in_progress),
         "sub": f"{active} agent{'s' if active > 1 else ''} actif{'s' if active > 1 else ''}"},
        {"label": "Corrections à valider", "value": str(len(pending)),
         "sub": "décision humaine requise" if pending else "aucune décision requise"},
        {"label": "Dernière analyse", "value": hhmm(last) if last else "—",
         "sub": day_label(last) if last else "aucune analyse"},
    ]


# ---------------------------------------------------------------- enquête d'un incident
def _hyp_key(case_id: str, h: dict) -> str:
    return f"{case_id}:{h['cause_type']}:{h['cause_key']}"


def _hyp_text(h: dict, confirmed: bool) -> str:
    reason = (h.get("details") or {}).get("reason")
    if h["status"] == "supported":
        share = f"Part de l'écart expliquée : {pct(h['explained_share'])}. " if h.get("explained_share") is not None else ""
        if confirmed:
            return share + "Une correction testée sur une copie fait disparaître le signal : la cause est démontrée."
        return share + ("Le lien est mesuré par la requête ci-dessous ; il ne devient une cause confirmée "
                        "qu'après une correction vérifiée sur une copie.")
    base = {"refuted": "Les données mesurées contredisent cette hypothèse.",
            "unlikely": "Rendue peu probable par les autres causes mesurées.",
            "untestable": "Les données nécessaires pour la tester ne sont pas disponibles.",
            "inconclusive": "Test exécuté, résultat non discriminant."}.get(h["status"], "")
    return f"{base} {reason}".strip() if reason else base


def _evidence(e: dict) -> dict:
    return {"description": e["description"], "sql": fmt_sql(e["sql"]), "rows": e["rows"],
            "result": e["result"][:8], "rerunnable": "?" not in e["sql"]}


def hypotheses_view(state: dict, inc: dict, ann: dict) -> list[dict]:
    rc_status = {rc["id"]: rc["assessment"]["status"] for rc in state.get("root_causes", [])}
    cases = [c for c in state.get("cases", []) if c["id"] in inc["case_ids"]]
    own = inc["rc"]["id"] if inc["rc"] else None
    # Les causes étayées d'autres incidents sont présentées dans leur propre enquête ; les résultats
    # identiques d'un même type de test sur plusieurs jours sont regroupés en une seule carte.
    best: dict[str, tuple[str, dict]] = {}
    repeats: dict[str, int] = {}
    for case in cases:
        for h in case["hypotheses"]:
            cause = f"{h['cause_type']}:{h['cause_key']}"
            if cause != own and cause in rc_status and h["status"] == "supported":
                continue
            k = cause if h["status"] == "supported" else f"{h['cause_type']}:{h['status']}"
            prev = best.get(k)
            repeats[k] = repeats.get(k, 0) + 1
            if prev is None or (HYP_ORDER.get(h["status"], 9), -(h.get("explained_share") or 0)) < \
                    (HYP_ORDER.get(prev[1]["status"], 9), -(prev[1].get("explained_share") or 0)):
                best[k] = (case["id"], h)
    tests = {}
    for ah in ann["hypotheses"]:
        if ah.get("about"):
            tests[ah["about"]] = ah
    out = []
    for group, (case_id, h) in sorted(best.items(), key=lambda x: (HYP_ORDER.get(x[1][1]["status"], 9),
                                                                    -(x[1][1].get("explained_share") or 0))):
        key = _hyp_key(case_id, h)
        more = repeats[group] - 1
        confirmed = h["status"] == "supported" and rc_status.get(f"{h['cause_type']}:{h['cause_key']}") == CONFIRMED
        if key in ann["dismissed"]:
            badge = ("Écartée", "neutral")
        elif confirmed:
            badge = ("Confirmée", "confirmed")
        elif h["status"] == "supported":
            badge = ("Hypothèse : à confirmer", "hypothesis")
        else:
            badge = (HYP_BADGES.get(h["status"], STATUS_LABELS.get(h["status"], h["status"])), "neutral")
        out.append({"key": key, "case_id": case_id, "statement": lc_first(h["statement"]), "badge": badge,
                    "text": _hyp_text(h, confirmed) + (f" Même résultat pour {more} autre(s) jour(s) de l'incident."
                                                       if more else ""), "evidence": [_evidence(e) for e in h["evidence"]],
                    "dismissed": ann["dismissed"].get(key), "analyst_test": _analyst_test(tests.get(key)),
                    "kind": "agent"})
    for ah in ann["hypotheses"]:
        if ah.get("about") or ah["case_id"] not in inc["case_ids"]:
            continue
        key = f"{ah['case_id']}:analyst:{ah['id']}"
        tested = ah["tested"] and not ah["tested"].get("error")
        badge = ("Écartée", "neutral") if key in ann["dismissed"] else \
            (("Hypothèse : à confirmer", "hypothesis") if tested else ("Non testée", "neutral"))
        out.append({"key": key, "id": ah["id"], "case_id": ah["case_id"], "statement": lc_first(ah["statement"]),
                    "badge": badge, "text": f"Ajoutée par {ah['author']}. Seule une correction vérifiée sur une copie "
                                            "peut confirmer une cause.",
                    "evidence": [], "sql": ah["sql"], "dismissed": ann["dismissed"].get(key),
                    "analyst_test": _analyst_test(ah), "kind": "analyst"})
    return out


def _analyst_test(ah: dict | None) -> dict | None:
    if not ah or not ah.get("tested"):
        return None
    t = ah["tested"]
    return {"sql": ah["sql"], "rows": t.get("rows"), "error": t.get("error"), "time": hhmm(t["ts"])}


def fact_view(svc: DataSentinel, state: dict, inc: dict) -> dict | None:
    """Fait observé : série quotidienne mesurée autour de la période de l'incident (table orders)."""
    if inc["table"] != "orders" or not state.get("detection", {}).get("as_of"):
        return {"title": "Fait observé : " + lc_first(inc["headline"]["summary"]),
                "text": f"Mesuré sur la table {inc['table']}.", "bars": None}
    revenue = inc["headline"]["kind"].startswith("revenue_") or inc["headline"]["kind"] == "price_outlier"
    flagged = {a["period_start"][:10] for a in inc["anomalies"] if a["kind"].startswith(("revenue_", "volume_"))}
    if not flagged:
        start, end = inc["period"]
        flagged = {(date.fromisoformat(start) + timedelta(days=i)).isoformat()
                   for i in range((date.fromisoformat(end) - date.fromisoformat(start)).days + 1)}
    last = min(max(date.fromisoformat(d) for d in flagged), date.fromisoformat(state["detection"]["as_of"]))
    first = max(min(date.fromisoformat(d) for d in flagged) - timedelta(days=7), last - timedelta(days=20))
    with Warehouse(state["db_path"]) as wh:
        daily = daily_metrics(wh, first, last)
    metric = "revenue" if revenue else "orders"
    bars = [{"day": d.date().isoformat(), "label": d.strftime("%d/%m"), "value": round(float(v), 2),
             "flagged": d.date().isoformat() in flagged} for d, v in daily[metric].items()]
    kinds = {a["kind"] for a in inc["anomalies"]}
    trend = "en baisse" if any(k.endswith("_drop") for k in kinds) else \
        "en hausse" if any(k.endswith("_spike") for k in kinds) else "concernée"
    return {"title": "Fait observé : " + ("chiffre d'affaires par jour" if revenue else "volume par jour"),
            "text": "Comparaison de la période concernée avec les jours précédents, mesurée sur la table orders.",
            "bars": bars, "unit": "€" if revenue else "commandes", "legend_flagged": f"Orange : période {trend}"}


def impact_view(inc: dict) -> dict:
    rc = inc["rc"]
    start, end = inc["period"]
    period = f"du {ddmm(start)} au {ddmm(end)}" if start != end else f"le {ddmm(start)}"
    if not rc or "impact" not in rc:
        return {"kpis": "non estimés (cause non déterminée)", "period": period, "decisions": "—", "dashboards": []}
    imp = rc["impact"]
    return {"kpis": ", ".join(lc_first(k["label"]) for k in imp["affected_kpis"]) or "aucun",
            "period": period, "decisions": ", ".join(lc_first(d) for d in imp["decisions"]) or "—",
            "dashboards": imp["dashboards"]}


def journal_view(state: dict | None) -> list[dict]:
    out = []
    for t in (state or {}).get("trace", []):
        agent = t["agent"].replace("Agent ", "")
        out.append({"time": hhmm(t["ts"]), "text": f"{agent} : {lc_first(t['summary'])}"})
    return out


# ---------------------------------------------------------------- rapport d'un incident
def report_view(state: dict, inc: dict, ann: dict) -> dict:
    rc, plan = inc["rc"], inc["plan"]
    start, end = inc["period"]
    period = f"du {ddmm(start)} au {ddmm(end)}" if start != end else f"le {ddmm(start)}"
    kpis = impact_view(inc)["kpis"]
    if rc and rc["assessment"]["status"] == CONFIRMED:
        summary = (f"{inc['title']} ({period}). Cause confirmée par une correction testée sur une copie : "
                   f"{lc_first(rc['statement'])}. Les indicateurs concernés ({kpis}) restent faux tant que la "
                   "correction n'est pas appliquée par votre équipe.")
    elif rc:
        summary = (f"{inc['title']} ({period}). Cause la plus probable, non confirmée : {lc_first(rc['statement'])}. "
                   f"Les indicateurs concernés ({kpis}) ne devraient pas servir à décider tant que la cause n'est pas "
                   "confirmée.")
    elif fresh := next((a for a in inc["anomalies"] if a["kind"] == "freshness"), None):
        summary = (f"{inc['title']} ({period}). {fresh['summary']} Les jours concernés ne contiennent aucune vente : "
                   "la baisse du chiffre d'affaires vient de l'absence de données, pas d'une baisse des ventes. "
                   "Les indicateurs de la période ne devraient pas servir à décider tant que les données ne sont pas "
                   "chargées ; la cause amont du retard n'est pas déterminée par les données disponibles.")
    else:
        summary = (f"{inc['title']} ({period}). Aucune cause n'a pu être établie à partir des données disponibles : "
                   "une investigation manuelle est nécessaire avant toute décision.")

    facts = [{"badge": ("Observée", "observed"), "text": a["summary"]} for a in inc["anomalies"][:5]]
    if len(inc["anomalies"]) > 5:
        facts.append({"badge": ("Observée", "observed"),
                      "text": f"… et {len(inc['anomalies']) - 5} autre(s) signal(aux) observé(s) relié(s)."})
    uncertainties = list(rc["assessment"]["uncertainties"]) if rc else []
    if rc:
        confirmed = rc["assessment"]["status"] == CONFIRMED
        why = ("la correction testée sur une copie fait disparaître le signal" if confirmed else
               "le lien est mesuré mais pas encore démontré par une correction vérifiée")
        facts.append({"badge": ("Confirmée", "confirmed") if confirmed else ("Hypothèse", "hypothesis"),
                      "text": f"{rc['statement'].rstrip('.')}. Confiance : {rc['assessment']['confidence']}, car {why}."})
    for h in hypotheses_view(state, inc, ann):
        if h["badge"][0] in ("Incertain", "Non testée"):
            uncertainties.append(f"{h['statement'][:1].upper()}{h['statement'][1:]} : hypothèse non exclue "
                                 f"({h['badge'][0].lower()}).")
    facts += [{"badge": ("Incertain", "neutral"), "text": u} for u in dict.fromkeys(uncertainties)]

    proofs = []
    if rc:
        for h in rc["hypotheses"]:
            for e in h["evidence"]:
                proofs.append(_evidence(e))
    else:
        proofs = [_evidence(a["evidence"]) for a in inc["anomalies"]]
    proofs = list({p["sql"]: p for p in proofs}.values())

    v = (rc or {}).get("verification") or {}
    decision = None
    if plan:
        entry = next((e for e in reversed(state.get("review_log", [])) if e["plan_id"] == plan["id"]), None)
        if entry:
            decision = {"decision": entry["decision"], "by": entry["reviewer"], "comment": entry.get("comment"),
                        "time": f"{local(entry['ts']):%d/%m à %H:%M}"}
    plan_state = ("none" if not plan else "approved" if plan.get("decision") == "approved" else
                  "rejected" if plan.get("decision") == "rejected" else "open")
    status_txt = {"none": "aucun plan de correction", "approved": "correction approuvée",
                  "rejected": "correction refusée", "open": "prêt pour validation"}[plan_state]
    narrative = (rc or {}).get("narrative")
    return {
        "subtitle": f"{inc['title']} · table {inc['table']} · {status_txt}",
        "summary": summary,
        "narrative": {"text": narrative["text"], "by": f"{narrative['provider']} / {narrative['model']}"}
        if narrative else None,
        "facts": facts, "proofs": proofs,
        "plan": {"id": plan["id"], "title": plan["title"], "steps": plan["steps"], "sql": plan["sql"],
                 "risk": plan["risk"], "rollback": plan["rollback"], "state": plan_state,
                 "executable": bool(plan.get("executable_in_sandbox") and plan.get("sql")),
                 "not_executable_reason": plan.get("not_executable_reason")} if plan else None,
        "verification": {"executed": v.get("executed"), "passed": v.get("passed"), "reason": v.get("reason"),
                         "checks": [{"name": c["name"], "passed": c["passed"], "observed": str(c["observed"])}
                                    for c in v.get("checks", [])]} if v else None,
        "before": inc["headline"]["summary"],
        "decision": decision,
        "can_decide": bool(plan) and plan_state == "open" and state.get("awaiting_review") and not ann["paused"],
        "report_ready": "report" in state,
    }


# ---------------------------------------------------------------- lancement des enquêtes
def launch_run(svc: DataSentinel, db: Path | None, checks: list[str] | None, actor: str,
               refresh_sql: bool = False) -> str:
    """Lance une enquête en arrière-plan. refresh_sql : extrait d'abord un instantané frais de la base
    SQL Server reliée (repli sur `db` si la base est injoignable)."""
    run_id = svc.new_run_id()
    with _RUNS_LOCK:
        _RUNS[run_id] = {"done": False, "error": None, "warning": None, "db_path": str(db) if db else None}

    def work():
        target = db
        try:
            if refresh_sql:
                try:
                    target = svc.sqlserver_extract(actor=actor)
                    _RUNS[run_id]["db_path"] = str(target)
                except Exception as exc:
                    if target is None:
                        raise RuntimeError(f"base SQL Server injoignable ({type(exc).__name__} : {exc})") from exc
                    _RUNS[run_id]["warning"] = (f"Base SQL Server injoignable ({type(exc).__name__}) : "
                                                "analyse du dernier instantané disponible.")
            svc.start(target, actor=actor, checks=checks, run_id=run_id)
        except Exception as exc:  # l'erreur est affichée dans la carte de l'agent concerné
            _RUNS[run_id]["error"] = f"{type(exc).__name__} : {exc}"
        finally:
            _RUNS[run_id]["done"] = True

    threading.Thread(target=work, daemon=True, name=f"enquete-{run_id}").start()
    return run_id


def sql_snapshots(svc: DataSentinel) -> list[Path]:
    """Instantanés de la base SQL Server reliée (configurée dans .env), du plus ancien au plus récent."""
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", f"sqlserver_{svc.settings.sqlserver.database}")
    root = svc.settings.sources_dir / slug
    return sorted(root.glob("*/warehouse.duckdb")) if root.exists() else []


def _same(a: str | None, b: Path) -> bool:
    return bool(a) and Path(a).resolve() == b.resolve()


def default_source(svc: DataSentinel, runs: list[dict]) -> tuple[str | None, str | None]:
    """Source par défaut quand aucun fichier n'est choisi : dernière analyse de la base SQL Server reliée.
    Lance l'analyse initiale (et l'extraction si aucun instantané n'existe) une seule fois."""
    with _RUNS_LOCK:
        pending = _DEFAULT["run_id"]
    if pending:
        info = _RUNS[pending]
        if not info["done"] or info["error"]:
            return pending, info["db_path"]
    snaps = sql_snapshots(svc)
    if snaps:
        latest = snaps[-1]
        analysed = next((r["run_id"] for r in runs if _same(r.get("db_path"), latest)), None)
        if analysed:
            return analysed, str(latest.resolve())
    with _RUNS_LOCK:
        if _DEFAULT["run_id"] != pending:  # une autre requête vient de lancer l'analyse initiale
            return _DEFAULT["run_id"], _RUNS[_DEFAULT["run_id"]]["db_path"]
        db = snaps[-1].resolve() if snaps else None
        _DEFAULT["run_id"] = launch_run(svc, db, None, "analyse-initiale", refresh_sql=db is None)
        return _DEFAULT["run_id"], str(db) if db else None


# ---------------------------------------------------------------- chiffre d'affaires : baisse ou non, et pourquoi
def _days(days: list[str]) -> str:
    labels = [ddmm(d) for d in sorted(set(days))]
    if len(labels) > 4:
        return f"du {labels[0]} au {labels[-1]} ({len(labels)} jours)"
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " et " + labels[-1]


def _signed(x: float | None) -> str:
    return "" if x is None else f"{'+' if x >= 0 else '−'}{round(abs(x) * 100)} %"


def revenue_view(state: dict, incs: list[dict]) -> dict | None:
    """Dit si le chiffre d'affaires baisse, de combien, quels jours, et quelle cause l'explique (avec son niveau de preuve)."""
    det = state.get("detection", {})
    if not det.get("as_of"):
        return None
    as_of, start = date.fromisoformat(det["as_of"]), date.fromisoformat(det["window_start"])
    with Warehouse(state["db_path"]) as wh:
        daily = with_baseline(daily_metrics(wh, start - timedelta(days=35), as_of))
    win = daily[daily.index >= pd.Timestamp(start)].dropna(subset=["revenue_expected"])
    observed, expected = float(win["revenue"].sum()), float(win["revenue_expected"].sum())
    net = (observed - expected) / expected if expected else None

    owners: dict[str, list[dict]] = {}
    for inc in incs:
        for a in inc["anomalies"]:
            owners.setdefault(a["id"], []).append(inc)

    def groups(kind: str) -> list[dict]:
        out: dict[tuple, dict] = {}
        for a in det.get("anomalies", []):
            if a["kind"] != kind:
                continue
            key = tuple(i["id"] for i in owners.get(a["id"], []))
            g = out.setdefault(key, {"incs": owners.get(a["id"], []), "days": [], "obs": 0.0, "exp": 0.0})
            g["days"].append(a["period_start"][:10])
            g["obs"] += a["observed"]
            g["exp"] += a["expected"] or 0.0
        result = []
        for g in out.values():
            causes = []
            for inc in g["incs"] or [None]:
                rc = inc["rc"] if inc else None
                if rc and rc["assessment"]["status"] == CONFIRMED:
                    causes.append({"level": "confirmed", "label": "Confirmée",
                                   "text": f"Cause confirmée par une correction testée sur une copie : {lc_first(rc['statement'])}."})
                elif rc:
                    causes.append({"level": "hypothesis", "label": "Hypothèse",
                                   "text": f"Cause probable, non confirmée : {lc_first(rc['statement'])}."})
                elif inc and (fresh := next((x for x in inc["anomalies"] if x["kind"] == "freshness"), None)):
                    causes.append({"level": "observed", "label": "Observée",
                                   "text": f"Aucune vente enregistrée ces jours-là. {fresh['summary']} Les données ne sont "
                                           "pas chargées depuis : la baisse vient de l'absence de données, pas des ventes. "
                                           "La cause amont (retard ou arrêt du chargement) n'est pas déterminée par les "
                                           "données disponibles."})
                else:
                    causes.append({"level": "observed", "label": "Observée",
                                   "text": "Cause non déterminée : aucune hypothèse n'est étayée par les données."})
            change = (g["obs"] - g["exp"]) / g["exp"] if g["exp"] else None
            result.append({"days": _days(g["days"]), "change": _signed(change),
                           "detail": f"{fmt_eur(g['obs'])} observés contre {fmt_eur(g['exp'])} attendus",
                           "causes": causes, "incident": g["incs"][0]["id"] if g["incs"] else None})
        return result

    drops, spikes = groups("revenue_drop"), groups("revenue_spike")
    if drops:
        verdict, title = "drop", "oui, il baisse anormalement"
    elif net is not None and net <= -0.05:
        verdict, title = "soft", "léger recul, sans anomalie détectée"
    elif spikes:
        verdict, title = "spike", "pas de baisse, mais des hausses anormales"
    else:
        verdict, title = "stable", "non, aucune baisse anormale"
    summary = (f"Du {ddmm(start.isoformat())} au {ddmm(as_of.isoformat())} : {fmt_eur(observed)} observés contre "
               f"{fmt_eur(expected)} attendus ({_signed(net)}).")
    if drops and net is not None and net >= 0:
        summary += " Sur l'ensemble de la période, les baisses sont masquées par des hausses anormales."
    return {"verdict": verdict, "title": f"Chiffre d'affaires : {title}", "summary": summary,
            "drops": drops, "spikes": spikes,
            "note": "Référence : médiane des 4 mêmes jours de semaine précédents. Une cause n'est « Confirmée » "
                    "qu'après une correction vérifiée sur une copie."}


# ---------------------------------------------------------------- état complet pour l'interface
def _incident_brief(i: dict) -> dict:
    return {k: i[k] for k in ("id", "title", "subtitle", "table", "severity", "level", "resolved")} | \
        {"severity_label": SEVERITIES[i["severity"]], "case_ids": i["case_ids"]}


def build_state(svc: DataSentinel, run_id: str | None, db_path: str | None, incident: str | None) -> dict:
    runs = svc.runs()
    default = not run_id and not db_path
    if default:
        run_id, db_path = default_source(svc, runs)
    if not run_id:
        same = [r for r in runs if db_path and Path(r.get("db_path", "")).resolve() == Path(db_path).resolve()]
        run_id = (same or ([] if db_path else runs) or [{}])[0].get("run_id")
    info = _RUNS.get(run_id or "", {})
    running, error = bool(info) and not info.get("done"), info.get("error")
    state = None
    if run_id:
        try:
            state = svc.get(run_id)
        except KeyError:
            if not info:
                run_id = None
    db_path = (state or {}).get("db_path") or info.get("db_path") or db_path
    ann = svc.annotations(run_id) if run_id else {"hypotheses": [], "dismissed": {}, "paused": False}
    incs = incidents(state) if state and not running else []
    current = next((i for i in incs if i["id"] == incident), incs[0] if incs else None)
    run_ts = next((r["ts"] for r in runs if r["run_id"] == run_id), None)
    pending = [p for p in (state or {}).get("plans", []) if p.get("decision") not in ("approved", "rejected")] \
        if state and state.get("awaiting_review") and not running else []
    first_pending = next((i["id"] for i in incs if i["plan"] and i["plan"]["id"] in {p["id"] for p in pending}), None)
    out = {
        "run_id": run_id, "db_path": db_path, "source": source_label(db_path, svc),
        "source_kind": source_kind(db_path) or ("sqlserver" if default else None), "default_source": default,
        "running": running, "error": error, "warning": info.get("warning"),
        "revenue": revenue_view(state, incs) if state and not running else None,
        "paused": ann["paused"], "status": run_label(state, ann["paused"], running),
        "skipped": (state or {}).get("detection", {}).get("skipped", []),
        "kpis": overview_kpis(svc, state, incs, runs), "incidents": [_incident_brief(i) for i in incs],
        "pending": {"count": len(pending), "incident": first_pending},
        "agents": agents_view(state, running, error), "journal": journal_view(state),
        "incident": None,
    }
    if current:
        when = "aujourd'hui" if run_ts and local(run_ts).date() == date.today() else \
            (f"le {local(run_ts):%d/%m}" if run_ts else "")
        detected = f"détectée {when} à {hhmm(run_ts)}" if run_ts else "détectée"
        out["incident"] = _incident_brief(current) | {
            "heading": f"Enquête : {lc_first(current['title'])}",
            "subtitle": f"Table {current['table']} · {detected} · {out['status']}",
            "hypotheses": hypotheses_view(state, current, ann), "fact": fact_view(svc, state, current),
            "impact": impact_view(current), "report": report_view(state, current, ann),
        }
    return out


# ---------------------------------------------------------------- profilage
def _type_label(name: str, col: dict, rows: int) -> str:
    n, dtype = name.lower(), col["dtype"]
    if n == "id" or n.endswith("_id"):
        return "Identifiant"
    if "datetime" in dtype or "date" in n or n.endswith("_at"):
        return "Date"
    if dtype == "bool" or dtype == "boolean":
        return "Booléen"
    if "stats" in col:
        if re.search(r"amount|price|cost|margin|revenue|montant|prix|cout|marge", n):
            return "Montant"
        return "Nombre"
    if col["distinct"] <= max(20, rows * 0.05):
        return "Catégorie"
    return "Texte"


def _alert(name: str, col: dict, prof: dict, type_label: str) -> str:
    if name == prof.get("key") and (prof.get("duplicate_keys") or 0) > 0:
        return "Doublons possibles"
    st = col.get("stats") or {}
    if type_label == "Montant" and st:
        lo, p99, hi = st.get("min"), st.get("99%"), st.get("max")
        if (lo is not None and lo < 0) or (p99 and hi is not None and p99 > 0 and hi > 10 * p99):
            return "Distribution à contrôler"
    return "Aucune"


def profile_view(svc: DataSentinel, db_path: str) -> list[dict]:
    out = []
    for prof in svc.profile(db_path):
        cols = []
        for c in prof["columns"]:
            t = _type_label(c["name"], c, prof["rows"])
            cols.append({"name": c["name"], "type": t,
                         "missing": f"{c['null_rate'] * 100:.1f} %".replace(".0 %", " %").replace(".", ","),
                         "distinct": f"{c['distinct']:,}".replace(",", " "), "alert": _alert(c["name"], c, prof, t)})
        out.append({"table": prof["table"], "rows": prof["rows"], "columns": cols})
    return out


# ---------------------------------------------------------------- routes
def _db(path: str | None, svc: DataSentinel) -> Path:
    if not path:
        raise HTTPException(400, "Aucune source sélectionnée.")
    p = Path(path).resolve()
    if not p.is_relative_to(svc.settings.home.resolve()) or p.suffix != ".duckdb" or not p.exists():
        raise HTTPException(404, "Base DuckDB introuvable dans l'espace de travail.")
    return p


def _get(svc: DataSentinel, run_id: str) -> dict:
    try:
        return svc.get(run_id)
    except KeyError:
        raise HTTPException(404, "Enquête inconnue.")


@router.get("/state")
def state(run_id: str | None = None, db_path: str | None = None, incident: str | None = None,
          svc: DataSentinel = Depends(get_service)):
    if db_path:
        db_path = str(_db(db_path, svc))
    return build_state(svc, run_id, db_path, incident)


class LabRequest(BaseModel):
    scenario: str = "all"


@router.post("/sources/lab")
def load_lab(req: LabRequest, svc: DataSentinel = Depends(get_service)):
    if req.scenario not in SCENARIOS:
        raise HTTPException(400, f"Scénario inconnu : {req.scenario}")
    db = str(svc.create_lab(req.scenario))
    return {"db_path": db, "source": source_label(db, svc)}


@router.get("/source")
def source(db_path: str, svc: DataSentinel = Depends(get_service)):
    db = str(_db(db_path, svc))
    return {"db_path": db, "source": source_label(db, svc), "source_kind": source_kind(db)}


@router.post("/sources/upload")
async def upload(files: list[UploadFile] = File(...), sep: str = Form("auto"), svc: DataSentinel = Depends(get_service)):
    separator = {"auto": None, "comma": ",", "semicolon": ";"}.get(sep)
    content = {Path(f.filename or "table").stem: await f.read() for f in files}
    try:
        db = str(svc.import_csv(content, sep=separator))
    except Exception as exc:
        raise HTTPException(400, f"Import impossible : {exc}")
    return {"db_path": db, "source": source_label(db, svc)}


@router.get("/sources/sqlserver/test")
def sqlserver_test(svc: DataSentinel = Depends(get_service)):
    try:
        info = svc.sqlserver_test()
    except Exception as exc:
        raise HTTPException(502, f"Connexion impossible : {type(exc).__name__} — {exc}")
    
    if not info.get("ok", True):
        return info

    return {
        k: info[k]
        for k in (
            "server_name",
            "database_name",
            "login_name",
            "version",
            "warning",
        )
        if k in info
    }



@router.post("/sources/sqlserver/extract")
def sqlserver_extract(svc: DataSentinel = Depends(get_service)):
    try:
        db = str(svc.sqlserver_extract())
    except Exception as exc:
        raise HTTPException(502, f"Extraction impossible : {exc}")
    return {"db_path": db, "source": source_label(db, svc)}


@router.get("/profile")
def profile(db_path: str, svc: DataSentinel = Depends(get_service)):
    return profile_view(svc, str(_db(db_path, svc)))


class StartRequest(BaseModel):
    db_path: str | None = None  # None : base SQL Server reliée
    checks: list[str] | None = None
    actor: str = "analyste"


@router.post("/runs")
def start_run(req: StartRequest, svc: DataSentinel = Depends(get_service)):
    """Sur la base SQL Server reliée, un instantané frais est extrait avant l'analyse."""
    checks = req.checks if req.checks is not None and set(req.checks) != set(CHECKS) else None
    if req.checks is not None and not req.checks:
        raise HTTPException(400, "Cochez au moins un contrôle.")
    if req.db_path:
        db = _db(req.db_path, svc)
        refresh = source_kind(str(db)) == "sqlserver"
    else:
        snaps = sql_snapshots(svc)
        db, refresh = (snaps[-1].resolve() if snaps else None), True
    run_id = launch_run(svc, db, checks, req.actor or "analyste", refresh_sql=refresh)
    if not req.db_path:
        with _RUNS_LOCK:
            _DEFAULT["run_id"] = run_id
    return {"run_id": run_id}


class Actor(BaseModel):
    actor: str = "analyste"


class PauseRequest(Actor):
    paused: bool


@router.post("/runs/{run_id}/pause")
def pause(run_id: str, req: PauseRequest, svc: DataSentinel = Depends(get_service)):
    _get(svc, run_id)
    svc.set_paused(run_id, req.paused, req.actor)
    return {"paused": req.paused}


class HypothesisRequest(Actor):
    case_id: str
    statement: str
    sql: str = ""
    about: str | None = None
    run_test: bool = False


def _run_sql(svc: DataSentinel, run_id: str, sql: str, actor: str, hypothesis_id: str | None = None) -> dict:
    try:
        df = svc.rerun_query(run_id, sql, actor, hypothesis_id=hypothesis_id)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(400, f"Requête impossible : {exc}")
    return {"rows": len(df), "result": records(df, 8), "columns": list(df.columns)}


@router.post("/runs/{run_id}/hypotheses")
def add_hypothesis(run_id: str, req: HypothesisRequest, svc: DataSentinel = Depends(get_service)):
    try:
        hyp = svc.add_hypothesis(run_id, req.case_id, req.statement, req.sql, req.actor, about=req.about)
    except KeyError:
        raise HTTPException(404, "Enquête inconnue.")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    out = {"id": hyp["id"]}
    if req.run_test and hyp["sql"]:
        out["test"] = _run_sql(svc, run_id, hyp["sql"], req.actor, hyp["id"])
    return out


@router.post("/runs/{run_id}/hypotheses/{hyp_id}/test")
def test_hypothesis(run_id: str, hyp_id: str, req: Actor, svc: DataSentinel = Depends(get_service)):
    _get(svc, run_id)
    hyp = next((h for h in svc.annotations(run_id)["hypotheses"] if h["id"] == hyp_id), None)
    if not hyp or not hyp["sql"]:
        raise HTTPException(400, "Hypothèse sans requête de test.")
    return _run_sql(svc, run_id, hyp["sql"], req.actor, hyp_id)


class DismissRequest(Actor):
    key: str
    reason: str


@router.post("/runs/{run_id}/dismiss")
def dismiss(run_id: str, req: DismissRequest, svc: DataSentinel = Depends(get_service)):
    try:
        svc.dismiss_hypothesis(run_id, req.key, req.reason, req.actor)
    except KeyError:
        raise HTTPException(404, "Enquête inconnue.")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


class RerunRequest(Actor):
    sql: str


@router.post("/runs/{run_id}/rerun")
def rerun(run_id: str, req: RerunRequest, svc: DataSentinel = Depends(get_service)):
    _get(svc, run_id)
    return _run_sql(svc, run_id, req.sql, req.actor)


class PlanTestRequest(Actor):
    plan_id: str
    sql: str | None = None


@router.post("/runs/{run_id}/plans/test")
def plan_test(run_id: str, req: PlanTestRequest, svc: DataSentinel = Depends(get_service)):
    state = _get(svc, run_id)
    plan = next((p for p in state.get("plans", []) if p["id"] == req.plan_id), None)
    if not plan:
        raise HTTPException(404, "Plan inconnu.")
    try:
        r = svc.test_on_copy(run_id, req.plan_id, plan["sql"] if req.sql is None else req.sql, req.actor)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(400, str(exc))
    return {"executed": r["executed"], "passed": r["passed"], "reason": r.get("reason"),
            "checks": [{"name": c["name"], "passed": c["passed"], "observed": str(c["observed"])} for c in r["checks"]]}


class DecisionRequest(Actor):
    plan_id: str
    decision: str
    comment: str = ""
    sql: str | None = None


@router.post("/runs/{run_id}/plans/decide")
def decide(run_id: str, req: DecisionRequest, svc: DataSentinel = Depends(get_service)):
    if req.decision not in ("approve", "reject", "modify"):
        raise HTTPException(400, "Décision attendue : approve, reject ou modify.")
    if req.decision in ("approve", "reject") and not req.comment.strip():
        raise HTTPException(400, "Un commentaire de décision est obligatoire pour approuver ou refuser.")
    state = _get(svc, run_id)
    plan = next((p for p in state.get("plans", []) if p["id"] == req.plan_id), None)
    if not plan:
        raise HTTPException(404, "Plan inconnu.")
    if req.decision == "modify" and (req.sql or "").strip() == (plan["sql"] or "").strip():
        raise HTTPException(400, "Modifiez la requête de correction avant de soumettre le plan modifié.")
    try:
        new = svc.review(run_id, {req.plan_id: {"decision": req.decision, "sql": req.sql,
                                                "comment": req.comment.strip() or None}}, req.actor)
    except (ValueError, PermissionError, RuntimeError) as exc:
        raise HTTPException(409, str(exc))
    return {"status": new["status"]}


def _events(svc: DataSentinel, run_id: str | None, queries: bool) -> list[dict]:
    events = svc.audit.events(run_id or None)
    return events if queries else [e for e in events if e["kind"] != "query"]


@router.get("/audit")
def audit(run_id: str | None = None, queries: bool = False, svc: DataSentinel = Depends(get_service)):
    return [{"time": f"{local(e['ts']):%d/%m/%Y %H:%M:%S}", "run_id": e["run_id"], "actor": e["actor"],
             "kind": e["kind"], "detail": json.dumps(e["payload"], ensure_ascii=False, default=str)[:300]}
            for e in reversed(_events(svc, run_id, queries))]


@router.get("/audit/export")
def audit_export(run_id: str | None = None, queries: bool = True, svc: DataSentinel = Depends(get_service)):
    body = json.dumps(_events(svc, run_id, queries), ensure_ascii=False, indent=2, default=str)
    name = f"journal_{run_id or 'complet'}.json"
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/runs/{run_id}/report.{fmt}")
def report_download(run_id: str, fmt: str, svc: DataSentinel = Depends(get_service)):
    if fmt not in ("json", "html"):
        raise HTTPException(404, "Format inconnu.")
    _get(svc, run_id)
    try:
        body = svc.report_json(run_id) if fmt == "json" else svc.report_html(run_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return Response(body, media_type="application/json" if fmt == "json" else "text/html",
                    headers={"Content-Disposition": f'attachment; filename="rapport_{run_id}.{fmt}"'})


@router.get("/settings")
def settings(svc: DataSentinel = Depends(get_service)):
    s = svc.settings
    sq = s.sqlserver
    return {"llm": f"Claude — {s.llm_model}" if s.llm_provider == "anthropic" else "gabarit local",
            "sqlserver": {"server": sq.server, "database": sq.database, "driver": sq.driver,
                          "auth": "authentification Windows" if sq.trusted_connection else f"utilisateur {sq.user}"},
            "scenarios": [{"id": k, "label": k} for k in SCENARIOS], "checks": CHECKS}


@router.post("/llm/check")
def llm_check(svc: DataSentinel = Depends(get_service)):
    return svc.llm_check()


@router.post("/evaluation")
def run_evaluation():
    ev = evaluate()
    return {"metrics": ev["metrics"],
            "rows": [{"scenario": r["scenario"], "incident": i["incident"], "detected": i["detected"],
                      "diagnosed": i["diagnosed"], "status": i["status"]}
                     for r in ev["scenarios"] for i in r["incidents"]]}
