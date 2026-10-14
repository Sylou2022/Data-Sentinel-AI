import json

import pytest

from datasentinel.evaluation import score
from datasentinel.generator import load_ground_truth


def test_workflow_pauses_for_human_review(service, lab):
    state = service.start(lab("all"))
    assert state["status"] == "awaiting_review"
    assert "report" not in state
    agents = [t["agent"] for t in state["trace"]]
    assert agents == ["Agent Détective", "Agent Enquêteur", "Agent Impact", "Agent Correcteur", "Agent Vérificateur"]


def test_confirmation_requires_sandbox_verification(service, lab):
    state = service.start(lab("all"))
    status = {rc["cause_type"]: rc["assessment"]["status"] for rc in state["root_causes"]}
    assert status["duplicate_load"] == "confirmed"
    assert status["price_inconsistency"] == "confirmed"
    assert status["missing_categories"] == "confirmed"
    # sans fichier source, un chargement partiel ou retardé ne peut pas être confirmé
    assert status["partial_load"] == "probable"
    assert status["late_load"] == "probable"
    residual = {a["kind"] for a in state["residual"]["residual_anomalies"]}
    assert "duplicate_keys" not in residual and "price_outlier" not in residual
    assert "freshness" in residual


def test_review_approve_and_reject_produces_report(service, lab):
    state = service.start(lab("all"))
    plans = [p["id"] for p in state["plans"]]
    decisions = {pid: {"decision": "approve"} for pid in plans}
    decisions[plans[0]] = {"decision": "reject", "comment": "à revoir"}
    state = service.review(state["run_id"], decisions, "relecteur.test")
    assert state["status"] == "completed"
    report = json.loads(service.report_json(state["run_id"]))
    assert report["summary"]["root_causes"] == 5
    assert {e["decision"] for e in report["review"]} == {"approve", "reject"}
    assert "<html" in service.report_html(state["run_id"])
    script = service.approved_script(state["run_id"])
    rejected = next(p for p in state["plans"] if p["id"] == plans[0])
    assert rejected["title"] not in script
    kinds = {e["kind"] for e in service.audit.events(state["run_id"])}
    assert {"run_started", "agent_step", "query", "human_decision", "narrative", "report_built"} <= kinds


def test_modified_plan_is_retested_before_report(service, lab):
    state = service.start(lab("duplicate_orders"))
    plan = state["plans"][0]
    # modification volontairement inefficace : le vérificateur doit le constater
    state = service.review(state["run_id"], {plan["id"]: {"decision": "modify", "sql": "SELECT 1;"}}, "relecteur.test")
    assert state["status"] == "awaiting_review"
    rc = state["root_causes"][0]
    assert rc["verification"]["passed"] is False
    assert rc["assessment"]["status"] == "probable"


def test_review_rejects_forbidden_sql(service, lab):
    state = service.start(lab("duplicate_orders"))
    plan = state["plans"][0]
    with pytest.raises(PermissionError):
        service.review(state["run_id"], {plan["id"]: {"decision": "modify", "sql": "COPY orders TO 'x.csv'"}}, "x")


def test_baseline_completes_without_review(service, lab):
    state = service.start(lab("baseline"))
    assert state["status"] == "completed"
    assert state["report"]["summary"]["anomalies"] == 0


@pytest.mark.parametrize("scenario", ["partial_load", "duplicate_orders", "price_error", "late_load", "missing_categories", "all"])
def test_scores_against_ground_truth(service, lab, scenario):
    db = lab(scenario)
    result = score(service.start(db), load_ground_truth(db))
    assert all(i["detected"] and i["diagnosed"] for i in result["incidents"])
    assert result["false_alerts"] == []
