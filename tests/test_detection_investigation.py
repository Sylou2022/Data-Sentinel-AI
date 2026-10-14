import pytest

from datasentinel.detection import make_context, run_detection
from datasentinel.investigation import Investigator, aggregate_root_causes
from datasentinel.models import SUPPORTED
from datasentinel.warehouse import Warehouse

EXPECTED = {
    "partial_load": ("partial_load", {"volume_drop"}),
    "duplicate_orders": ("duplicate_load", {"duplicate_keys", "volume_spike"}),
    "price_error": ("price_inconsistency", {"price_outlier", "revenue_spike"}),
    "late_load": ("late_load", {"freshness", "volume_drop"}),
    "missing_categories": ("missing_categories", {"missing_category"}),
}


def _investigate(db, settings):
    with Warehouse(db) as wh:
        det = run_detection(wh, settings)
        cases = Investigator(wh, make_context(wh, settings)).investigate(det["anomalies"])
    rcs, unexplained = aggregate_root_causes(cases)
    return det, cases, rcs, unexplained


def test_baseline_produces_no_false_alert(lab, settings):
    det, _, rcs, _ = _investigate(lab("baseline"), settings)
    assert det["anomalies"] == []
    assert rcs == []


@pytest.mark.parametrize("scenario", list(EXPECTED))
def test_single_incident_is_detected_and_diagnosed(lab, settings, scenario):
    cause_type, kinds = EXPECTED[scenario]
    det, cases, rcs, unexplained = _investigate(lab(scenario), settings)
    assert kinds <= {a["kind"] for a in det["anomalies"]}
    assert [rc["cause_type"] for rc in rcs] == [cause_type], "une seule cause, la bonne"
    assert unexplained == []


def test_combined_scenario_finds_all_five_causes(lab, settings):
    _, _, rcs, unexplained = _investigate(lab("all"), settings)
    assert sorted(rc["cause_type"] for rc in rcs) == sorted(
        ["partial_load", "duplicate_load", "price_inconsistency", "late_load", "missing_categories"])
    assert unexplained == []


def test_partial_load_is_found_despite_success_status(lab, settings):
    _, cases, _, _ = _investigate(lab("partial_load"), settings)
    hyp = next(h for c in cases for h in c["hypotheses"] if h["cause_type"] == "partial_load" and h["status"] == SUPPORTED)
    assert hyp["details"]["silent_failure"] is True
    assert hyp["details"]["missing_rows"] > 0
    assert hyp["evidence"] and all(e["sql"] for e in hyp["evidence"])


def test_business_variation_is_never_silently_dismissed(lab, settings):
    _, cases, _, _ = _investigate(lab("partial_load"), settings)
    alt = next(h for c in cases for h in c["hypotheses"] if h["cause_type"] == "business_variation")
    assert alt["status"] in ("unlikely", "inconclusive")
    assert alt["details"]["reason"]


def test_competing_hypotheses_are_refuted_with_evidence(lab, settings):
    _, cases, _, _ = _investigate(lab("late_load"), settings)
    partial = [h for c in cases for h in c["hypotheses"] if h["cause_type"] == "partial_load"]
    assert partial and all(h["status"] == "refuted" for h in partial)
