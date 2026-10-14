"""Meridian Retail : jeu de données e-commerce synthétique et reproductible.

Cinq incidents connus peuvent être injectés ; chacun produit une entrée de
« vérité de référence » (ground truth) qui sert à évaluer les diagnostics.
Aucune donnée personnelle : tous les identifiants sont fictifs.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

CATEGORIES = ["Électronique", "Maison", "Sport", "Mode", "Beauté", "Jardin", "Jouets", "Librairie"]
REGIONS = ["Île-de-France", "Auvergne-Rhône-Alpes", "PACA", "Occitanie", "Hauts-de-France", "Nouvelle-Aquitaine"]
SEGMENTS = ["Particulier", "PME", "Grand compte"]
STATUSES = ["completed", "cancelled", "returned"]
DOW_FACTOR = [1.0, 1.0, 1.0, 1.05, 1.15, 0.85, 0.75]  # lundi -> dimanche

INCIDENTS = ["partial_load", "duplicate_orders", "price_error", "late_load", "missing_categories"]
SCENARIOS = ["baseline", *INCIDENTS, "all"]
TABLES = ["orders", "products", "products_snapshot", "customers", "pipeline_logs"]


@dataclass(frozen=True)
class LabConfig:
    seed: int = 42
    as_of: date = date(2026, 9, 30)
    n_days: int = 120
    n_products: int = 80
    n_customers: int = 600
    daily_orders: int = 400


@dataclass
class GroundTruth:
    incident: str
    cause_type: str  # doit correspondre aux types de cause de l'enquêteur
    table: str
    dates: list[str]
    key: str  # date ou identifiant produit qui caractérise la cause
    expected_effect: str


def _batch_time(d: date, hour: int = 23, minute: int = 30) -> datetime:
    return datetime.combine(d, time(hour, minute))


def _base_tables(cfg: LabConfig, rng: np.random.Generator):
    start = cfg.as_of - timedelta(days=cfg.n_days - 1)

    prices = np.clip(np.round(np.exp(rng.normal(np.log(60), 0.6, cfg.n_products)), 2), 5, 600)
    products = pd.DataFrame({
        "product_id": [f"PRD-{i:04d}" for i in range(1, cfg.n_products + 1)],
        "product_name": [f"Produit {i:03d}" for i in range(1, cfg.n_products + 1)],
        "category": [CATEGORIES[i % len(CATEGORIES)] for i in range(cfg.n_products)],
        "price": prices,
        "cost": np.round(prices * rng.uniform(0.55, 0.75, cfg.n_products), 2),
        "updated_at": pd.Timestamp(start),
    })
    popularity = rng.gamma(2.0, 1.0, cfg.n_products)
    popularity /= popularity.sum()

    customers = pd.DataFrame({
        "customer_id": [f"CUST-{i:05d}" for i in range(1, cfg.n_customers + 1)],
        "segment": rng.choice(SEGMENTS, cfg.n_customers, p=[0.7, 0.22, 0.08]),
        "region": rng.choice(REGIONS, cfg.n_customers),
    })

    frames = []
    for offset in range(cfg.n_days):
        d = start + timedelta(days=offset)
        n = int(rng.poisson(cfg.daily_orders * DOW_FACTOR[d.weekday()]))
        prod_idx = rng.choice(cfg.n_products, n, p=popularity)
        qty = rng.choice([1, 2, 3, 4], n, p=[0.5, 0.3, 0.15, 0.05])
        discount = rng.choice([0.0, 0.0, 0.0, 0.05, 0.10], n)
        unit_price = np.round(prices[prod_idx] * (1 - discount), 2)
        frames.append(pd.DataFrame({
            "order_date": pd.Timestamp(d),
            "customer_id": customers["customer_id"].to_numpy()[rng.integers(0, cfg.n_customers, n)],
            "product_id": products["product_id"].to_numpy()[prod_idx],
            "quantity": qty,
            "unit_price": unit_price,
            "status": rng.choice(STATUSES, n, p=[0.92, 0.05, 0.03]),
            "loaded_at": pd.Timestamp(_batch_time(d)) + pd.Timedelta(minutes=int(rng.integers(5, 25))),
            "batch_id": f"orders_{d.isoformat()}",
        }))
    orders = pd.concat(frames, ignore_index=True)
    orders.insert(0, "order_id", [f"ORD-{i:07d}" for i in range(1, len(orders) + 1)])
    orders["amount"] = np.round(orders["quantity"] * orders["unit_price"], 2)

    logs = []
    for offset in range(cfg.n_days):
        d = start + timedelta(days=offset)
        n = int((orders["order_date"] == pd.Timestamp(d)).sum())
        logs.append(_log(f"orders_{d.isoformat()}", "orders", d, "success", n, n))
        logs.append(_log(f"products_{d.isoformat()}", "products", d, "success", cfg.n_products, cfg.n_products,
                         started=_batch_time(d, 23, 0)))
    return products, customers, orders, logs, popularity


def _log(run_id, table, d, status, expected, loaded, started=None, finished=None, error=None):
    started = started or _batch_time(d)
    return {
        "run_id": run_id,
        "table_name": table,
        "batch_date": pd.Timestamp(d),
        "started_at": pd.Timestamp(started),
        "finished_at": pd.Timestamp(finished or started + timedelta(minutes=12)) if status != "delayed" else pd.NaT,
        "status": status,
        "rows_expected": expected,
        "rows_loaded": loaded,
        "error_message": error,
    }


def _replace_log(logs, run_id, **changes):
    for entry in logs:
        if entry["run_id"] == run_id:
            entry.update(changes)
            return entry
    raise KeyError(run_id)


def generate(scenario: str = "all", cfg: LabConfig | None = None) -> tuple[dict[str, pd.DataFrame], list[GroundTruth]]:
    if scenario not in SCENARIOS:
        raise ValueError(f"Scénario inconnu : {scenario}. Choix : {SCENARIOS}")
    cfg = cfg or LabConfig()
    rng = np.random.default_rng(cfg.seed)
    products, customers, orders, logs, popularity = _base_tables(cfg, rng)
    active = INCIDENTS if scenario == "all" else ([] if scenario == "baseline" else [scenario])
    truth: list[GroundTruth] = []
    A = cfg.as_of

    # Instantané du référentiel produits (J-8), conservé comme dans un entrepôt réel.
    snapshot = products.copy()
    snapshot["snapshot_date"] = pd.Timestamp(A - timedelta(days=8))

    if "price_error" in active:
        d = A - timedelta(days=12)
        order = np.argsort(-popularity)
        idx = next(i for i in order[15:] if 25 <= products.loc[i, "price"] <= 70)
        pid = products.loc[idx, "product_id"]
        products.loc[idx, ["price", "updated_at"]] = [round(products.loc[idx, "price"] * 100, 2), pd.Timestamp(d)]
        mask = (orders["product_id"] == pid) & (orders["order_date"] >= pd.Timestamp(d))
        orders.loc[mask, "unit_price"] = np.round(orders.loc[mask, "unit_price"] * 100, 2)
        orders.loc[mask, "amount"] = np.round(orders.loc[mask, "quantity"] * orders.loc[mask, "unit_price"], 2)
        # Le prix de l'instantané J-8 est aussi contaminé (incident antérieur à l'instantané).
        snapshot.loc[snapshot["product_id"] == pid, "price"] = products.loc[idx, "price"]
        truth.append(GroundTruth("price_error", "price_inconsistency", "products",
                                 [(d + timedelta(days=k)).isoformat() for k in range(13)], pid,
                                 "Prix unitaire multiplié par 100 (centimes chargés comme euros) : CA et marge surévalués."))

    if "duplicate_orders" in active:
        d = A - timedelta(days=9)
        batch = orders[orders["order_date"] == pd.Timestamp(d)].copy()
        batch["loaded_at"] = pd.Timestamp(_batch_time(d + timedelta(days=1), 1, 10))
        orders = pd.concat([orders, batch], ignore_index=True)
        rerun = _log(f"orders_{d.isoformat()}_rerun", "orders", d, "success", len(batch), len(batch),
                     started=_batch_time(d + timedelta(days=1), 1, 5),
                     error="Relance manuelle du lot sans purge préalable")
        logs.append(rerun)
        truth.append(GroundTruth("duplicate_orders", "duplicate_load", "orders", [d.isoformat()], d.isoformat(),
                                 "Lot chargé deux fois : volume et CA du jour doublés."))

    if "missing_categories" in active:
        d = A - timedelta(days=7)
        idx = products.sample(12, random_state=cfg.seed).index
        products.loc[idx, "category"] = None
        products.loc[idx, "updated_at"] = pd.Timestamp(d)
        _replace_log(logs, f"products_{d.isoformat()}", status="success_with_warnings",
                     error_message="Colonne 'category' absente pour 12 lignes du flux fournisseur v2 — chargées à NULL")
        truth.append(GroundTruth("missing_categories", "missing_categories", "products", [d.isoformat()], d.isoformat(),
                                 "12 produits sans catégorie : analyses par catégorie partielles."))

    if "partial_load" in active:
        d = A - timedelta(days=5)
        day_idx = orders.index[orders["order_date"] == pd.Timestamp(d)]
        keep = int(len(day_idx) * 0.38)
        orders = orders.drop(day_idx[keep:]).reset_index(drop=True)
        # Statut « success » : le chargement partiel est silencieux, seule la volumétrie le trahit.
        _replace_log(logs, f"orders_{d.isoformat()}", rows_loaded=keep)
        truth.append(GroundTruth("partial_load", "partial_load", "orders", [d.isoformat()], d.isoformat(),
                                 "Seules 38 % des lignes du fichier chargées : volume et CA du jour sous-estimés."))

    if "late_load" in active:
        late_days = [A - timedelta(days=1), A]
        for d in late_days:
            orders = orders[orders["order_date"] != pd.Timestamp(d)]
            _replace_log(logs, f"orders_{d.isoformat()}", status="delayed", rows_loaded=0, finished_at=pd.NaT,
                         error_message="Fichier source non livré par le SFTP avant le SLA de 23:30")
        orders = orders.reset_index(drop=True)
        truth.append(GroundTruth("late_load", "late_load", "orders", [d.isoformat() for d in late_days], "orders",
                                 "Derniers lots non chargés : indicateurs de la période courante incomplets."))

    tables = {
        "orders": orders.sort_values(["order_date", "order_id"]).reset_index(drop=True),
        "products": products,
        "products_snapshot": snapshot,
        "customers": customers,
        "pipeline_logs": pd.DataFrame(logs).sort_values(["batch_date", "started_at"]).reset_index(drop=True),
    }
    return tables, truth


def build_lab(scenario: str, out_dir: Path, cfg: LabConfig | None = None, write_csv: bool = True) -> Path:
    """Écrit le jeu de données dans une base DuckDB (+ CSV) et retourne le chemin de la base."""
    tables, truth = generate(scenario, cfg)
    out_dir = Path(out_dir) / scenario
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "warehouse.duckdb"
    if db_path.exists():
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    try:
        for name, df in tables.items():
            con.register("_df", df)
            con.execute(f"CREATE TABLE {name} AS SELECT * FROM _df")
            con.unregister("_df")
            if write_csv:
                df.to_csv(out_dir / f"{name}.csv", index=False)
    finally:
        con.close()
    (out_dir / "ground_truth.json").write_text(
        json.dumps([asdict(t) for t in truth], ensure_ascii=False, indent=2), encoding="utf-8")
    return db_path


def load_ground_truth(db_path: Path) -> list[dict]:
    path = Path(db_path).parent / "ground_truth.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Génère le jeu Meridian Retail")
    parser.add_argument("--scenario", default="all", choices=SCENARIOS + ["every"])
    parser.add_argument("--out", default="var/labs")
    args = parser.parse_args()
    for sc in (SCENARIOS if args.scenario == "every" else [args.scenario]):
        print(build_lab(sc, Path(args.out)))
