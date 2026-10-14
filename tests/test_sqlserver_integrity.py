"""Connecteur SQL Server et règles d'intégrité, sur un jeu reproduisant le schéma Assistant_IA_Entreprise
(Fact_Ventes et dimensions) et les 5 types d'anomalies de sa table DQ_Anomalies_Test."""
from datetime import date, timedelta

import duckdb
import numpy as np
import pandas as pd
import pytest

from datasentinel.config import SqlServerSettings
from datasentinel.connectors import DEFAULT_MAPPING, SqlServerSource, validate_select
from datasentinel.detection import run_detection
from datasentinel.warehouse import Warehouse

AS_OF = date(2026, 9, 30)
INJECTED = {"amount_missing": 10, "quantity_non_positive": 10, "cost_negative": 10, "margin_inconsistent": 10}


def _star_like(path, inject: bool):
    """Même forme que l'instantané extrait de SQL Server : CA aléatoire par vente (prix implicite très dispersé)."""
    rng = np.random.default_rng(7)
    n = 20000
    days = pd.to_datetime([AS_OF - timedelta(days=int(x)) for x in rng.integers(0, 365, n)])
    qty = rng.integers(1, 10, n)
    amount = rng.integers(100, 5000, n).astype(float)
    cost = rng.integers(100, 3500, n).astype(float)
    orders = pd.DataFrame({
        "order_id": [str(i) for i in range(1, n + 1)], "order_date": days, "customer_id": [str(x) for x in rng.integers(1, 501, n)],
        "product_id": [str(x) for x in rng.integers(1, 51, n)], "region_id": [str(x) for x in rng.integers(1, 11, n)],
        "quantity": qty, "amount": amount, "unit_price": amount / qty, "cost": cost, "margin": amount - cost,
        "status": "completed"})
    products = pd.DataFrame({"product_id": [str(i) for i in range(1, 51)], "product_name": [f"P{i}" for i in range(1, 51)],
                             "category": "Informatique", "sub_category": "x", "price": rng.integers(50, 1500, 50).astype(float)})
    customers = pd.DataFrame({"customer_id": [str(i) for i in range(1, 501)], "segment": "Standard",
                              "customer_status": "Actif",
                              "signup_date": pd.to_datetime([AS_OF - timedelta(days=int(x)) for x in rng.integers(30, 900, 500)])})
    if inject:
        idx = rng.choice(n, 40, replace=False)
        a, q, c, m = idx[:10], idx[10:20], idx[20:30], idx[30:40]
        orders.loc[a, ["amount", "margin"]] = np.nan
        orders.loc[q, "quantity"] = -orders.loc[q, "quantity"]
        orders.loc[c, "cost"] = -orders.loc[c, "cost"]
        orders.loc[c, "margin"] = orders.loc[c, "amount"] - orders.loc[c, "cost"]  # marge cohérente avec le coût faux
        orders.loc[m, "margin"] = orders.loc[m, "margin"] + 999
        customers.loc[:4, "signup_date"] = pd.Timestamp(AS_OF + timedelta(days=40))
    path.unlink(missing_ok=True)
    con = duckdb.connect(str(path))
    for name, df in {"orders": orders, "products": products, "customers": customers}.items():
        con.register("_df", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _df")
        con.unregister("_df")
    con.close()
    return path


@pytest.fixture
def star(settings):
    return lambda inject: _star_like(settings.home / f"star_{inject}.duckdb", inject)


def test_clean_star_schema_has_no_false_alert(star, settings):
    with Warehouse(star(False)) as wh:
        det = run_detection(wh, settings)
    assert det["anomalies"] == [], [a["id"] for a in det["anomalies"]]


def test_injected_dq_anomalies_are_detected(star, settings):
    with Warehouse(star(True)) as wh:
        det = run_detection(wh, settings)
    found = {a["metric"]: a["observed"] for a in det["anomalies"] if a["kind"] == "integrity"}
    for rule, count in INJECTED.items():
        assert found.get(rule) == count, (rule, found)
    assert found.get("signup_in_future") == 5
    assert not any(a["kind"] == "price_outlier" for a in det["anomalies"]), "prix implicite dispersé : pas de fausse alerte"


def test_integrity_fixes_are_confirmed_on_copy(star, service):
    state = service.start(star(True))
    status = {rc["cause_key"]: rc["assessment"]["status"] for rc in state["root_causes"]}
    for rule in INJECTED:
        assert status[rule] == "confirmed", (rule, status)
    assert status["signup_in_future"] == "probable"  # pas de valeur de remplacement déductible
    residual = {a["id"] for a in state["residual"]["residual_anomalies"]}
    assert residual == {"integrity_signup_in_future"}


@pytest.mark.parametrize("sql", ["DELETE FROM dbo.Fact_Ventes", "SELECT * INTO x FROM dbo.Fact_Ventes",
                                 "SELECT 1; DROP TABLE dbo.Fact_Ventes", "EXEC sp_who", "UPDATE dbo.Dim_Client SET Statut='x'"])
def test_mapping_rejects_writes(sql):
    with pytest.raises(PermissionError):
        validate_select(sql)


def test_default_mapping_is_read_only():
    for sql in DEFAULT_MAPPING.values():
        validate_select(sql)


def test_connection_string_is_read_intent():
    cs = SqlServerSettings(server="srv", database="db", trusted_connection=True).connection_string()
    assert "ApplicationIntent=ReadOnly" in cs and "Trusted_Connection=yes" in cs and "PWD" not in cs


def _sqlserver_available() -> bool:
    try:
        SqlServerSource(SqlServerSettings()).test_connection()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _sqlserver_available(), reason="SQL Server local non disponible")
def test_real_sqlserver_extraction(settings):
    db = SqlServerSource(SqlServerSettings()).extract(settings.sources_dir)
    with Warehouse(db) as wh:
        assert {"orders", "products", "customers", "regions"} <= set(wh.tables())
        assert int(wh.query("SELECT count(*) AS n FROM orders")["n"].iloc[0]) > 0
