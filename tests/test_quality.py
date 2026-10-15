"""Contrôles de qualité génériques : vérité terrain, précision / rappel, faux positifs, corrections sûres."""
import numpy as np
import pandas as pd
from datasentinel.quality import ERROR, detect_quality
from datasentinel.warehouse import Warehouse, import_report, load_csv_workspace

rng = np.random.default_rng(42)


def _ventes(n=500):
    df = pd.DataFrame({"order_id": [f"ORD{i:04d}" for i in range(n)], "quantity": rng.integers(1, 100, n),
                       "unit_price": rng.uniform(5, 200, n).round(2), "discount_rate": rng.choice([0, 0.05, 0.10, 0.15], n),
                       "region": rng.choice(["Nord", "Sud", "Est", "Ouest"], n)})
    df["revenue"] = (df["quantity"] * df["unit_price"] * (1 - df["discount_rate"])).round(2)
    return df


def _clients(n=200):
    return pd.DataFrame({"customer_id": [f"C{i:04d}" for i in range(n)], "email": [f"client{i}@example.com" for i in range(n)],
                         "age": rng.integers(18, 80, n), "lifetime_value": rng.uniform(10, 5000, n).round(2)})


def _stocks(n=150):
    return pd.DataFrame({"sku": [f"SKU{i:04d}" for i in range(n)], "stock_quantity": rng.integers(0, 500, n),
                         "unit_cost": rng.uniform(1, 300, n).round(2), "reorder_threshold": rng.integers(5, 50, n)})


def _kpi():
    return pd.DataFrame({"month": pd.date_range("2022-01-01", periods=36, freq="MS").strftime("%d/%m/%Y"),
                         "revenue": 100000 + np.arange(36) * 1500.0, "orders": rng.integers(1000, 3000, 36),
                         "costs": rng.uniform(50000, 80000, 36).round(2)})


def _corrupt():
    """Jeux à anomalies connues : (table, index, colonne) attendus = vérité terrain."""
    v, c, s, k = _ventes(), _clients(), _stocks(), _kpi()
    truth = set()
    for df, name, edits in [
        (v, "ventes", [(5, "quantity", -10), (20, "unit_price", 0), (35, "discount_rate", 1.5), (50, "revenue", 999999),
                       (70, "region", None), (100, "order_id", "ORD0099")]),
        (c, "clients", [(5, "email", "email_invalide"), (10, "age", 240), (15, "lifetime_value", -500),
                        (20, "customer_id", "C0019"), (25, "email", None)]),
        (s, "stocks", [(3, "stock_quantity", -12), (8, "unit_cost", 0), (12, "reorder_threshold", -1),
                       (20, "sku", "SKU0019"), (30, "unit_cost", None)]),
        (k, "kpi", [(20, "revenue", 800000.0), (24, "orders", -150), (28, "costs", None)]),
    ]:
        for i, col, val in edits:
            df[col] = df[col].astype(object)
            df.at[i, col] = val
            truth.add((name, i + 2, col))  # numéro de ligne dans le fichier (en-tête = ligne 1)
            if col in ("order_id", "customer_id", "sku"):  # identifiant dupliqué : les deux lignes sont concernées
                truth.add((name, i + 1, col))
    return {"ventes": v, "clients": c, "stocks": s, "kpi": k}, truth


def _run(frames, tmp_path, sep=";"):
    files = {f"{n}.csv": df.to_csv(index=False, sep=sep).encode("utf-8") for n, df in frames.items()}
    db = load_csv_workspace(files, tmp_path / "w" / "warehouse.duckdb")
    reports = {r["table"]: r for r in import_report(db)}
    with Warehouse(db) as wh:
        found, skipped = detect_quality(wh, wh.tables(), reports)
    assert not skipped
    return found


def _cells(found):
    return {(a.table, r["ident"], r["column"]) for a in found for r in a.details["rows"]}


def test_recall_and_precision_on_known_anomalies(tmp_path):
    frames, truth = _corrupt()
    found = _run(frames, tmp_path)
    cells = _cells(found)
    recall = len(truth & cells) / len(truth)
    precision = len(truth & cells) / len(cells)
    assert recall == 1.0, sorted(truth - cells)
    assert precision >= 0.9, sorted(cells - truth)


def test_clean_files_raise_no_confirmed_error(tmp_path):
    found = _run({"ventes": _ventes(), "clients": _clients(), "stocks": _stocks(), "kpi": _kpi()}, tmp_path, sep=",")
    assert [a.summary for a in found if a.details["status"] == ERROR] == []
    assert len(found) == 0, [a.summary for a in found]


def test_each_finding_is_explained_and_traceable(tmp_path):
    frames, _ = _corrupt()
    for a in _run(frames, tmp_path):
        d = a.details
        assert d["status"] in ("Erreur confirmée", "Anomalie potentielle")
        assert d["justification"] and d["limits"] and d["recommendation"] and d["confidence"] and d["method"]
        assert d["rows"] and all("ident" in r and "column" in r and "value" in r for r in d["rows"])
        assert a.evidence.sql.startswith("SELECT") and a.evidence.rows == d["count"]


def test_relation_derives_proposed_values_from_the_data(tmp_path):
    frames, _ = _corrupt()
    rel = next(a for a in _run(frames, tmp_path) if a.kind == "quality_relation")
    expected = {r["ident"]: (r["column"], r["expected"]) for r in rel.details["rows"]}
    assert expected[7][0] == "quantity" and expected[7][1] > 0          # quantité négative : recalculée
    assert expected[37][0] == "discount_rate" and 0 <= expected[37][1] <= 1  # taux impossible : recalculé
    assert expected[52][0] == "revenue"                                  # CA aberrant : recalculé
    assert "UPDATE" in rel.details["correction"]["sql"]


def test_proposed_corrections_never_touch_the_source(tmp_path, service):
    frames, _ = _corrupt()
    files = {f"{n}.csv": df.to_csv(index=False).encode() for n, df in frames.items()}
    db = service.import_csv(files)
    state = service.start(db)
    rel = next(a for a in state["detection"]["anomalies"] if a["kind"] == "quality_relation")
    plan_id = "q:" + rel["id"]
    changes = service.plan_changes(state["run_id"], plan_id)
    assert {c["action"] for t in changes for c in t["changes"]} == {"modifiée"}
    test = service.test_finding_on_copy(state["run_id"], plan_id, service.finding_sql(state["run_id"], plan_id), "test")
    assert test["executed"] and test["passed"]
    with Warehouse(db) as wh:  # la source est inchangée
        assert wh.query("SELECT max(revenue) AS m FROM ventes")["m"].iloc[0] == 999999
    service.decide_finding(state["run_id"], plan_id, "approve", "test", "vérifié")
    assert "UPDATE" in service.approved_script(state["run_id"])
