"""Évaluation contre la vérité de référence : taux de détection, fausses alertes,
justesse du diagnostic, temps d'enquête — pour chaque scénario du jeu Meridian Retail."""
from __future__ import annotations

import time
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver

from .config import Settings, get_settings
from .generator import SCENARIOS, load_ground_truth


def _overlaps(anomaly: dict, dates: list[str]) -> bool:
    return any(anomaly["period_start"] <= d <= anomaly["period_end"] for d in dates)


def _matches(rc: dict, gt: dict) -> bool:
    if rc["cause_type"] != gt["cause_type"]:
        return False
    return gt["key"] in rc["cause_key"].split(",") or gt["cause_type"] == "late_load"


def score(state: dict, truth: list[dict]) -> dict:
    anomalies = state.get("detection", {}).get("anomalies", [])
    rcs = state.get("root_causes", [])
    incidents = []
    for gt in truth:
        detected = any(_overlaps(a, gt["dates"]) or (gt["cause_type"] == "missing_categories" and a["kind"] == "missing_category")
                       for a in anomalies)
        match = next((rc for rc in rcs if _matches(rc, gt)), None)
        incidents.append({"incident": gt["incident"], "detected": detected, "diagnosed": match is not None,
                          "status": match["assessment"]["status"] if match and "assessment" in match else None})
    false_causes = [rc["id"] for rc in rcs if not any(_matches(rc, gt) for gt in truth)]
    false_alerts = false_causes + [c["id"] for c in state.get("unexplained", [])]
    if not truth:
        false_alerts = [a["id"] for a in anomalies]
    return {"incidents": incidents, "false_alerts": false_alerts}


def evaluate(scenarios: list[str] | None = None, settings: Settings | None = None) -> dict:
    from .service import DataSentinel

    settings = settings or get_settings()
    ds = DataSentinel(settings, checkpointer=MemorySaver())
    rows = []
    for sc in scenarios or SCENARIOS:
        db = ds.create_lab(sc)
        t0 = time.perf_counter()
        state = ds.start(db, actor="evaluation")
        elapsed = time.perf_counter() - t0
        result = score(state, load_ground_truth(Path(db)))
        rows.append({"scenario": sc, "seconds": round(elapsed, 2), **result})
    all_inc = [i for r in rows for i in r["incidents"]]
    return {
        "scenarios": rows,
        "metrics": {
            "incidents": len(all_inc),
            "detection_rate": round(sum(i["detected"] for i in all_inc) / len(all_inc), 3) if all_inc else None,
            "diagnosis_accuracy": round(sum(i["diagnosed"] for i in all_inc) / len(all_inc), 3) if all_inc else None,
            "confirmed_rate": round(sum(i["status"] == "confirmed" for i in all_inc) / len(all_inc), 3) if all_inc else None,
            "false_alerts": sum(len(r["false_alerts"]) for r in rows),
            "mean_investigation_seconds": round(sum(r["seconds"] for r in rows) / len(rows), 2),
        },
    }


if __name__ == "__main__":
    import json
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
