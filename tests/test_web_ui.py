import time

import pytest
from fastapi.testclient import TestClient

import api.main as api_main


@pytest.fixture
def client(service):
    api_main.app.dependency_overrides[api_main.get_service] = lambda: service
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def _wait(client, run_id, timeout=120):
    for _ in range(timeout * 4):
        state = client.get("/ui/api/state", params={"run_id": run_id}).json()
        if not state["running"]:
            return state
        time.sleep(0.25)
    raise AssertionError("enquête trop longue")


def test_index_and_static_files_are_served(client):
    assert "Navigation principale" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_web_flow_lab_to_partial_validation(client, lab):
    db = str(lab("all"))
    profile = client.get("/ui/api/profile", params={"db_path": db}).json()
    orders = next(t for t in profile if t["table"] == "orders")
    assert {c["name"]: c["type"] for c in orders["columns"]}["order_id"] == "Identifiant"

    run_id = client.post("/ui/api/runs", json={"db_path": db}).json()["run_id"]
    state = _wait(client, run_id)
    assert [a["status"] for a in state["agents"]] == ["done"] * 4
    titles = {i["title"] for i in state["incidents"]}
    assert {"Doublons de commandes", "Prix produit incohérent", "Chargement retardé", "Catégories manquantes"} <= titles
    # un incident non confirmé n'est jamais présenté comme Confirmée
    late = next(i for i in state["incidents"] if i["title"] == "Chargement retardé")
    assert late["level"] == "hypothesis"

    dup = next(i for i in state["incidents"] if i["title"] == "Doublons de commandes")
    view = client.get("/ui/api/state", params={"run_id": run_id, "incident": dup["id"]}).json()["incident"]
    plan_id = view["report"]["plan"]["id"]
    assert view["report"]["can_decide"]
    r = client.post(f"/ui/api/runs/{run_id}/plans/decide", json={"plan_id": plan_id, "decision": "approve", "comment": ""})
    assert r.status_code == 400  # commentaire obligatoire
    r = client.post(f"/ui/api/runs/{run_id}/plans/decide",
                    json={"plan_id": plan_id, "decision": "approve", "comment": "Testé sur copie, OK."})
    assert r.json()["status"] == "awaiting_review"  # les autres plans attendent encore une décision
    after = client.get("/ui/api/state", params={"run_id": run_id, "incident": dup["id"]}).json()
    assert next(i for i in after["incidents"] if i["id"] == dup["id"])["resolved"]
    assert after["incident"]["report"]["decision"]["comment"] == "Testé sur copie, OK."
    assert client.get(f"/ui/api/runs/{run_id}/report.json").status_code == 409

    for inc in after["incidents"]:
        rep = client.get("/ui/api/state", params={"run_id": run_id, "incident": inc["id"]}).json()["incident"]["report"]
        if rep["can_decide"]:
            client.post(f"/ui/api/runs/{run_id}/plans/decide",
                        json={"plan_id": rep["plan"]["id"], "decision": "reject", "comment": "Hors périmètre."})
    final = client.get("/ui/api/state", params={"run_id": run_id}).json()
    assert final["status"] == "enquête terminée"
    assert client.get(f"/ui/api/runs/{run_id}/report.html").status_code == 200
    assert "attachment" in client.get("/ui/api/audit/export", params={"run_id": run_id}).headers["content-disposition"]


def test_selected_checks_limit_incidents(client, lab):
    db = str(lab("all"))
    run_id = client.post("/ui/api/runs", json={"db_path": db, "checks": ["categories"]}).json()["run_id"]
    state = _wait(client, run_id)
    assert [i["title"] for i in state["incidents"]] == ["Catégories manquantes"]


def test_without_source_overview_shows_linked_sql_database(client, service, tmp_path):
    """Sans fichier choisi : analyse initiale du dernier instantané de la base SQL Server reliée."""
    import json
    import shutil

    import api.ui as ui
    from datasentinel.generator import build_lab

    slug = "sqlserver_" + service.settings.sqlserver.database.replace(" ", "_")
    snap = service.settings.sources_dir / slug / "29991231T000000Z"
    snap.mkdir(parents=True, exist_ok=True)
    shutil.copy(build_lab("late_load", tmp_path, write_csv=False), snap / "warehouse.duckdb")
    (snap / "source.json").write_text(json.dumps({"server": "test", "database": service.settings.sqlserver.database,
                                                  "extracted_at": "29991231T000000Z", "tables": {}}), encoding="utf-8")
    ui._DEFAULT["run_id"] = None
    first = client.get("/ui/api/state").json()
    assert first["default_source"] and first["source_kind"] == "sqlserver"
    state = _wait(client, first["run_id"])
    assert state["source"].startswith("Source active : SQL Server")
    again = client.get("/ui/api/state").json()  # pas de nouvelle analyse : la dernière est réutilisée
    assert again["run_id"] == first["run_id"]
    rv = again["revenue"]
    assert rv["verdict"] == "drop" and rv["drops"]
    assert rv["drops"][0]["causes"][0]["label"] in ("Hypothèse", "Observée")  # jamais « Confirmée » sans vérification


def test_revenue_card_explains_drop_and_masking_spikes(client, lab):
    db = str(lab("all"))
    state = _wait(client, client.post("/ui/api/runs", json={"db_path": db}).json()["run_id"])
    rv = state["revenue"]
    assert rv["title"] == "Chiffre d'affaires : oui, il baisse anormalement"
    assert "masquées par des hausses anormales" in rv["summary"]
    assert any("retard" in c["text"] for g in rv["drops"] for c in g["causes"])
    assert any(c["label"] == "Confirmée" and "PRD-0026" in c["text"] for g in rv["spikes"] for c in g["causes"])
