"""Orchestration LangGraph des agents DataSentinel.

Détective → Enquêteur → Impact → Correcteur → Vérificateur → [validation humaine] → Rapporteur

Le graphe s'interrompt avant la validation humaine : le relecteur approuve, refuse ou modifie
chaque plan. Une modification renvoie le plan au Vérificateur pour un nouveau test sur copie.
L'état est persisté par un checkpointer (SQLite) : une enquête peut être reprise plus tard.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..audit import AuditLog
from ..config import Settings
from ..detection import is_quality, check_of, make_context, run_detection
from ..impact import ImpactAnalyst
from ..investigation import Investigator, aggregate_root_causes
from ..llm import get_narrator
from ..remediation import Verifier, propose_plan, validate_sandbox_sql
from ..report import build_report
from ..warehouse import Warehouse

REVIEW_NODE = "human_review"


class SentinelState(TypedDict, total=False):
    run_id: str
    db_path: str
    as_of: str | None
    checks: list[str] | None
    detection: dict
    cases: list[dict]
    root_causes: list[dict]
    unexplained: list[dict]
    plans: list[dict]
    residual: dict
    review: dict | None
    review_log: list[dict]
    needs_reverify: bool
    trace: list[dict]
    queries: list[dict]
    report: dict


def build_graph(settings: Settings, audit: AuditLog, checkpointer: Any):
    def _open(state: SentinelState):
        wh = Warehouse(state["db_path"])
        as_of = date.fromisoformat(state["as_of"]) if state.get("as_of") else None
        return wh, make_context(wh, settings, as_of)

    def _finish(state: SentinelState, wh: Warehouse, agent: str, summary: str, update: dict) -> dict:
        queries = [q.to_dict() for q in wh.log]
        for q in queries:
            q["agent"] = agent
        wh.close()
        audit.record(state["run_id"], agent, "agent_step", {"summary": summary, "queries": len(queries)})
        for q in queries:
            audit.record(state["run_id"], agent, "query", q)
        step = {"agent": agent, "summary": summary, "ts": datetime.now(timezone.utc).isoformat()}
        return {**update, "trace": state.get("trace", []) + [step], "queries": state.get("queries", []) + queries}

    def detective(state: SentinelState) -> dict:
        wh = Warehouse(state["db_path"])
        as_of = date.fromisoformat(state["as_of"]) if state.get("as_of") else None
        det = run_detection(wh, settings, as_of)
        if state.get("checks"):  # contrôles choisis par l'analyste : les autres familles sont ignorées
            det["anomalies"] = [a for a in det["anomalies"] if check_of(a["kind"]) in state["checks"]]
            det["checks"] = list(state["checks"])
        n = len(det["anomalies"])
        nq = sum(is_quality(a) for a in det["anomalies"])
        window = f" sur la fenêtre {det['window_start']} → {det['as_of']}" if det.get("as_of") else ""
        return _finish(state, wh, "Agent Détective",
                       f"{n} anomalie(s) détectée(s){window}, dont {nq} constat(s) de qualité des données.",
                       {"detection": det, "as_of": det.get("as_of")})

    def investigator(state: SentinelState) -> dict:
        wh, ctx = _open(state)
        inv = Investigator(wh, ctx)
        cases = inv.investigate([a for a in state["detection"]["anomalies"] if not is_quality(a)])
        rcs, unexplained = aggregate_root_causes(cases)
        n_tests = sum(len(c["hypotheses"]) for c in cases)
        return _finish(state, wh, "Agent Enquêteur",
                       f"{len(cases)} dossier(s), {n_tests} hypothèse(s) testée(s), {len(rcs)} cause(s) étayée(s), "
                       f"{len(unexplained)} dossier(s) sans cause.",
                       {"cases": cases, "root_causes": rcs, "unexplained": unexplained})

    def impact(state: SentinelState) -> dict:
        wh, ctx = _open(state)
        inv = Investigator(wh, ctx)
        analyst = ImpactAnalyst(wh, ctx, inv.avg_order_revenue, inv.daily)
        rcs = [{**rc, "impact": analyst.assess(rc)} for rc in state["root_causes"]]
        kpis = sorted({k["label"] for rc in rcs for k in rc["impact"]["affected_kpis"]})
        return _finish(state, wh, "Agent Impact", f"KPI potentiellement affectés : {', '.join(kpis) or 'aucun'}.",
                       {"root_causes": rcs})

    def remediation(state: SentinelState) -> dict:
        wh, _ = _open(state)
        plans = [propose_plan(rc, wh) for rc in state["root_causes"]]
        n_exec = sum(p["executable_in_sandbox"] for p in plans)
        return _finish(state, wh, "Agent Correcteur",
                       f"{len(plans)} plan(s) proposé(s), dont {n_exec} testable(s) sur copie.", {"plans": plans})

    def verifier(state: SentinelState) -> dict:
        wh, ctx = _open(state)
        ver = Verifier(wh, ctx)
        plans = {p["root_cause_id"]: p for p in state["plans"]}
        rcs = []
        for rc in state["root_causes"]:
            plan = plans[rc["id"]]
            verification = ver.test_plan(rc, plan) if plan.get("decision") != "rejected" else \
                {"plan_id": plan["id"], "executed": False, "checks": [], "passed": None, "reason": "Plan refusé par le relecteur."}
            rcs.append({**rc, "verification": verification,
                        "assessment": Verifier.classify(rc, verification, state["cases"])})
        residual = ver.residual_after_all(state["plans"])
        if state.get("checks"):
            residual["residual_anomalies"] = [a for a in residual["residual_anomalies"]
                                              if check_of(a["kind"]) in state["checks"]]
        confirmed = sum(r["assessment"]["status"] == "confirmed" for r in rcs)
        return _finish(state, wh, "Agent Vérificateur",
                       f"{confirmed}/{len(rcs)} cause(s) confirmée(s) par correction sur copie ; "
                       f"{len(residual['residual_anomalies'])} anomalie(s) résiduelle(s) après corrections.",
                       {"root_causes": rcs, "residual": residual, "needs_reverify": False})

    def human_review(state: SentinelState) -> dict:
        review = state.get("review") or {}
        reviewer = review.get("reviewer", "inconnu")
        decisions = review.get("decisions", {})
        log = list(state.get("review_log", []))
        plans, modified = [], False
        for plan in state["plans"]:
            d = decisions.get(plan["id"])
            if not d:
                plans.append(plan)
                continue
            plan = dict(plan)
            entry = {"ts": datetime.now(timezone.utc).isoformat(), "reviewer": reviewer, "plan_id": plan["id"],
                     "decision": d["decision"], "comment": d.get("comment")}
            if d["decision"] == "modify":
                validate_sandbox_sql(d.get("sql", ""))
                entry["previous_sql"], plan["sql"] = plan["sql"], d.get("sql", "")
                plan["decision"], modified = "modified_pending_retest", True
            else:
                plan["decision"] = {"approve": "approved", "reject": "rejected"}[d["decision"]]
            plan["reviewed_by"] = reviewer
            log.append(entry)
            audit.record(state["run_id"], reviewer, "human_decision", entry)
            plans.append(plan)
        return {"plans": plans, "review_log": log, "review": None, "needs_reverify": modified,
                "trace": state.get("trace", []) + [{"agent": f"Relecteur humain ({reviewer})",
                                                    "summary": f"{len(decisions)} décision(s) enregistrée(s).",
                                                    "ts": datetime.now(timezone.utc).isoformat()}]}

    def reporter(state: SentinelState) -> dict:
        narrator = get_narrator(settings)
        rcs = []
        for rc in state.get("root_causes", []):
            narrative = narrator.explain(rc)
            audit.record(state["run_id"], "Rapporteur", "narrative",
                         {"root_cause": rc["id"], "provider": narrative["provider"], "model": narrative["model"],
                          "guard": narrative["guard"]})
            rcs.append({**rc, "narrative": narrative})
        new_state = {**state, "root_causes": rcs}
        report = build_report(new_state)
        audit.record(state["run_id"], "Rapporteur", "report_built", report["summary"])
        return {"root_causes": rcs, "report": report}

    def after_detective(state: SentinelState) -> str:
        return "investigator" if any(not is_quality(a) for a in state["detection"]["anomalies"]) else "reporter"

    def after_verifier(state: SentinelState) -> str:
        return REVIEW_NODE if state.get("plans") else "reporter"

    def after_review(state: SentinelState) -> str:
        if state.get("needs_reverify"):
            return "verifier"
        # validation plan par plan : le rapport n'est produit qu'une fois chaque plan approuvé ou refusé
        if any(p.get("decision") not in ("approved", "rejected") for p in state.get("plans", [])):
            return REVIEW_NODE
        return "reporter"

    g = StateGraph(SentinelState)
    for name, fn in [("detective", detective), ("investigator", investigator), ("impact", impact),
                     ("remediation", remediation), ("verifier", verifier), (REVIEW_NODE, human_review),
                     ("reporter", reporter)]:
        g.add_node(name, fn)
    g.add_edge(START, "detective")
    g.add_conditional_edges("detective", after_detective, ["investigator", "reporter"])
    g.add_edge("investigator", "impact")
    g.add_edge("impact", "remediation")
    g.add_edge("remediation", "verifier")
    g.add_conditional_edges("verifier", after_verifier, [REVIEW_NODE, "reporter"])
    g.add_conditional_edges(REVIEW_NODE, after_review, ["verifier", REVIEW_NODE, "reporter"])
    g.add_edge("reporter", END)
    return g.compile(checkpointer=checkpointer, interrupt_before=[REVIEW_NODE])
