"""Accès aux données : lecture seule sur la source, bac à sable isolé pour les corrections.

Chaque requête exécutée est enregistrée (SQL, nombre de lignes, durée, empreinte du
résultat) afin que tout élément de preuve d'un rapport soit rejouable.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb
import pandas as pd

_READ_ONLY_SQL = re.compile(r"^\s*(with|select|describe|show|summarize|pragma\s+table_info)\b", re.IGNORECASE)


@dataclass
class QueryRecord:
    sql: str
    rows: int
    duration_ms: float
    result_hash: str
    target: str  # "source" ou "sandbox"

    def to_dict(self) -> dict:
        return asdict(self)


def _hash_df(df: pd.DataFrame) -> str:
    return hashlib.sha256(pd.util.hash_pandas_object(df, index=False).values.tobytes()).hexdigest()[:16]


class Warehouse:
    """Connexion DuckDB en lecture seule : toute écriture est refusée par le moteur."""

    target = "source"

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(self.db_path)
        self.con = duckdb.connect(str(self.db_path), read_only=True)
        self.log: list[QueryRecord] = []

    def query(self, sql: str, params: list | None = None) -> pd.DataFrame:
        if not _READ_ONLY_SQL.match(sql):
            raise PermissionError("Seules les requêtes de lecture sont autorisées sur la source.")
        return self._run(sql, params)

    def _run(self, sql: str, params: list | None = None) -> pd.DataFrame:
        t0 = time.perf_counter()
        df = self.con.execute(sql, params or []).df()
        self.log.append(QueryRecord(" ".join(sql.split()), len(df), round((time.perf_counter() - t0) * 1000, 2),
                                    _hash_df(df), self.target))
        return df

    def tables(self) -> list[str]:
        return self.con.execute("SELECT table_name FROM information_schema.tables ORDER BY 1").df()["table_name"].tolist()

    def has_table(self, name: str) -> bool:
        return name in self.tables()

    def sandbox(self) -> "Sandbox":
        return Sandbox(self)

    def close(self) -> None:
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Sandbox(Warehouse):
    """Copie en mémoire des tables source : les corrections y sont testées, jamais sur la source."""

    target = "sandbox"

    def __init__(self, source: Warehouse):
        self.db_path = source.db_path
        self.log = source.log  # journal partagé : la traçabilité couvre aussi le bac à sable
        self.con = duckdb.connect(":memory:")
        for name in source.tables():
            df = source.con.execute(f'SELECT * FROM "{name}"').df()
            self.con.register("_df", df)
            self.con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM _df')
            self.con.unregister("_df")
        # Une fois la copie faite, le bac à sable n'a plus accès au système de fichiers.
        self.con.execute("SET enable_external_access = false")

    def execute(self, sql: str) -> pd.DataFrame:
        return self._run(sql)

    def query(self, sql: str, params: list | None = None) -> pd.DataFrame:
        return self._run(sql, params)


IMPORT_REPORT = "import_report.json"


def load_csv_workspace(files: dict[str, bytes | str | Path], db_path: Path, sep: str | None = None) -> Path:
    """Crée une base DuckDB à partir de CSV importés (nom de fichier -> contenu), sans schéma imposé.

    sep=None : séparateur détecté automatiquement. Le rapport d'import (types inférés, ambiguïtés,
    lignes mal formées, aperçu) est écrit à côté de la base, dans import_report.json."""
    import json

    from .ingestion import read_csv

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    reports = []
    con = duckdb.connect(str(db_path))
    try:
        for name, content in files.items():
            if isinstance(content, Path):
                content = content.read_bytes()
            df, rep = read_csv(content, name if "." in name else name + ".csv", sep=sep)
            table, n = rep.table, 2
            while table in {r["table"] for r in reports}:
                table, n = f"{rep.table}_{n}", n + 1
            rep.table = table
            con.register("_df", df)
            con.execute(f'CREATE TABLE "{table}" AS SELECT * FROM _df')
            con.unregister("_df")
            reports.append(rep.to_dict())
    finally:
        con.close()
    (db_path.parent / IMPORT_REPORT).write_text(json.dumps(reports, ensure_ascii=False, default=str, indent=1),
                                                encoding="utf-8")
    return db_path


def import_report(db_path: str | Path) -> list[dict] | None:
    """Rapport d'import d'un espace de travail CSV (None pour une autre source)."""
    import json

    path = Path(db_path).parent / IMPORT_REPORT
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
