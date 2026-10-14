import pytest

from datasentinel.detection import check_of


def test_checks_restrict_detection(service, lab):
    state = service.start(lab("all"), checks=["categories"])
    kinds = {a["kind"] for a in state["detection"]["anomalies"]}
    assert kinds and {check_of(k) for k in kinds} == {"categories"}
    with pytest.raises(ValueError):
        service.start(lab("all"), checks=["inconnu"])


def test_pause_blocks_review(service, lab):
    state = service.start(lab("all"))
    run_id = state["run_id"]
    service.set_paused(run_id, True, "analyste")
    assert service.annotations(run_id)["paused"]
    with pytest.raises(RuntimeError):
        service.review(run_id, {p["id"]: {"decision": "approve"} for p in state["plans"]}, "relecteur")
    service.set_paused(run_id, False, "analyste")
    assert service.review(run_id, {p["id"]: {"decision": "approve"} for p in state["plans"]}, "relecteur")["status"] == "completed"


def test_analyst_hypothesis_and_dismissal_are_audited(service, lab):
    state = service.start(lab("all"))
    run_id, case = state["run_id"], state["cases"][0]
    hyp = service.add_hypothesis(run_id, case["id"], "Hypothèse de l'analyste", "SELECT count(*) AS n FROM orders", "ana")
    df = service.rerun_query(run_id, hyp["sql"], "ana", hypothesis_id=hyp["id"])
    assert len(df) == 1
    with pytest.raises(PermissionError):
        service.rerun_query(run_id, "DELETE FROM orders", "ana")
    with pytest.raises(ValueError):
        service.dismiss_hypothesis(run_id, "k", "  ", "ana")
    service.dismiss_hypothesis(run_id, f"{case['id']}:analyst:{hyp['id']}", "hors sujet", "ana")
    ann = service.annotations(run_id)
    assert ann["hypotheses"][0]["tested"]["rows"] == 1
    assert ann["dismissed"][f"{case['id']}:analyst:{hyp['id']}"]["reason"] == "hors sujet"
    # aucune action de l'analyste ne change le statut calculé des causes
    assert [r["assessment"] for r in service.get(run_id)["root_causes"]] == [r["assessment"] for r in state["root_causes"]]


def test_test_on_copy_does_not_record_decision(service, lab):
    state = service.start(lab("all"))
    plan = next(p for p in state["plans"] if p["sql"])
    result = service.test_on_copy(state["run_id"], plan["id"], plan["sql"], "ana")
    assert result["executed"] and result["passed"]
    after = service.get(state["run_id"])
    assert after["awaiting_review"] and not any(p.get("decision") for p in after["plans"])
