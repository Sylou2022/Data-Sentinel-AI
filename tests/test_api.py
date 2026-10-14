import pytest
from fastapi.testclient import TestClient

import api.main as api_main


@pytest.fixture
def client(service):
    api_main.app.dependency_overrides[api_main.get_service] = lambda: service
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def test_full_api_flow(client):
    assert client.get("/health").json()["status"] == "ok"
    r = client.post("/investigations", json={"scenario": "duplicate_orders"})
    assert r.status_code == 200
    run = r.json()
    assert run["awaiting_review"] is True
    assert client.get(f"/investigations/{run['run_id']}/report.json").status_code == 409

    decisions = {p["id"]: {"decision": "approve"} for p in run["plans"]}
    r = client.post(f"/investigations/{run['run_id']}/review", json={"reviewer": "api.test", "decisions": decisions})
    assert r.status_code == 200 and r.json()["status"] == "completed"
    assert client.get(f"/investigations/{run['run_id']}/report.json").json()["summary"]["confirmed"] == 1
    assert "BEGIN TRANSACTION" in client.get(f"/investigations/{run['run_id']}/approved.sql").text


def test_paths_outside_workspace_are_refused(client, tmp_path):
    outside = tmp_path / "x.duckdb"
    outside.write_bytes(b"")
    assert client.get("/datasets/profile", params={"db_path": str(outside)}).status_code == 404


def test_csv_upload_and_profile(client):
    csv = b"order_id,order_date,amount,status\nA,2026-01-01,10,completed\nA,2026-01-01,10,completed\n"
    r = client.post("/datasets/upload", files=[("files", ("orders.csv", csv, "text/csv"))])
    assert r.status_code == 200
    prof = client.get("/datasets/profile", params={"db_path": r.json()["db_path"]}).json()
    assert prof[0]["duplicate_keys"] == 1
