"""Connecteur SQL Server en lecture seule.

Principe : DataSentinel n'analyse jamais directement la base de production. Il exécute des
requêtes SELECT déclarées dans un *mapping* (schéma source → schéma canonique), dans une
transaction annulée à la fin, et écrit le résultat dans un instantané DuckDB local daté.
Détection, enquête et corrections travaillent ensuite sur cet instantané.

Le mapping par défaut correspond au modèle en étoile de la base Data_Sentinel_AI
(Fact_Ventes, Dim_Date, Dim_Produit, Dim_Client, Dim_Region). Pour une autre base, fournir un
fichier JSON {"table_canonique": "SELECT ..."} via SQLSERVER_MAPPING_FILE.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from ..config import SqlServerSettings

DEFAULT_MAPPING: dict[str, str] = {
    # Fact_Ventes n'a pas de statut de commande : toutes les ventes sont considérées comme réalisées.
    "orders": """
        SELECT CAST(f.Vente_ID AS varchar(20))   AS order_id,
               CAST(d.[Date] AS datetime2)       AS order_date,
               f.Date_ID                         AS date_key,
               CAST(f.Client_ID AS varchar(20))  AS customer_id,
               CAST(f.Produit_ID AS varchar(20)) AS product_id,
               CAST(f.Region_ID AS varchar(20))  AS region_id,
               f.Quantite                        AS quantity,
               CAST(f.Chiffre_Affaires AS float) AS amount,
               CAST(f.Chiffre_Affaires AS float) / NULLIF(f.Quantite, 0) AS unit_price,
               CAST(f.Cout AS float)             AS cost,
               CAST(f.Marge AS float)            AS margin,
               'completed'                       AS status
        FROM dbo.Fact_Ventes AS f
        LEFT JOIN dbo.Dim_Date AS d ON d.Date_ID = f.Date_ID
    """,
    "products": """
        SELECT CAST(Produit_ID AS varchar(20)) AS product_id,
               Nom_Produit                     AS product_name,
               Categorie                       AS category,
               Sous_Categorie                  AS sub_category,
               CAST(Prix_Unitaire AS float)    AS price
        FROM dbo.Dim_Produit
    """,
    "customers": """
        SELECT CAST(Client_ID AS varchar(20))    AS customer_id,
               Segment                           AS segment,
               Statut                            AS customer_status,
               CAST(Date_Inscription AS datetime2) AS signup_date
        FROM dbo.Dim_Client
    """,
    "regions": """
        SELECT CAST(Region_ID AS varchar(20)) AS region_id, Region AS region, Pays AS country
        FROM dbo.Dim_Region
    """,
}

_SELECT_ONLY = re.compile(r"^\s*(with|select)\b", re.IGNORECASE)
_FORBIDDEN = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|truncate|exec|execute|grant|revoke|into|"
                        r"openrowset|opendatasource|xp_\w+|sp_\w+)\b", re.IGNORECASE)


def load_mapping(settings: SqlServerSettings) -> dict[str, str]:
    if settings.mapping_file:
        return json.loads(Path(settings.mapping_file).read_text(encoding="utf-8"))
    return DEFAULT_MAPPING


def validate_select(sql: str) -> None:
    """Refuse toute requête qui ne serait pas une lecture simple."""
    if not _SELECT_ONLY.match(sql) or _FORBIDDEN.search(sql) or ";" in sql.strip().rstrip(";"):
        raise PermissionError("Le mapping SQL Server n'accepte que des requêtes SELECT uniques, sans écriture.")


class SqlServerSource:
    def __init__(self, settings: SqlServerSettings):
        self.settings = settings

    def _connect(self):
        import pyodbc  # dépendance optionnelle

        con = pyodbc.connect(self.settings.connection_string(), autocommit=False, timeout=10)
        con.timeout = 300
        return con

    def _read(self, con, sql: str) -> pd.DataFrame:
        validate_select(sql)
        cur = con.cursor()
        try:
            cur.execute(sql)
            cols = [c[0] for c in cur.description]
            return pd.DataFrame.from_records(cur.fetchall(), columns=cols)
        finally:
            cur.close()

    def test_connection(self) -> dict:
        con = self._connect()
        try:
            info = self._read(con, "SELECT @@SERVERNAME AS server_name, DB_NAME() AS database_name, "
                                   "SYSTEM_USER AS login_name, CAST(SERVERPROPERTY('ProductVersion') AS varchar(30)) AS version")
            perms = self._read(con, "SELECT permission_name FROM fn_my_permissions(NULL, 'DATABASE')")
            tables = self._read(con, """
                SELECT s.name AS schema_name, t.name AS table_name,
                       SUM(CASE WHEN p.index_id IN (0, 1) THEN p.rows ELSE 0 END) AS row_count
                FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
                JOIN sys.partitions p ON p.object_id = t.object_id
                WHERE t.is_ms_shipped = 0 GROUP BY s.name, t.name""")
        finally:
            con.rollback()
            con.close()
        granted = set(perms["permission_name"])
        write_perms = sorted(granted & {"INSERT", "UPDATE", "DELETE", "ALTER", "CONTROL", "CREATE TABLE"})
        return {
            **{k: str(v) for k, v in info.iloc[0].items()},
            "tables": tables.sort_values(["schema_name", "table_name"]).to_dict(orient="records"),
            "write_permissions": write_perms,
            "warning": ("Ce compte dispose de droits d'écriture (" + ", ".join(write_perms) + "). DataSentinel n'exécute "
                        "que des SELECT, mais un compte limité à db_datareader est recommandé.") if write_perms else None,
        }

    def extract(self, out_root: Path, mapping: dict[str, str] | None = None) -> Path:
        """Extrait les tables canoniques vers un instantané DuckDB et retourne son chemin."""
        mapping = mapping or load_mapping(self.settings)
        for sql in mapping.values():
            validate_select(sql)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        slug = re.sub(r"[^A-Za-z0-9_]+", "_", f"sqlserver_{self.settings.database}")
        out_dir = Path(out_root) / slug / stamp
        out_dir.mkdir(parents=True, exist_ok=True)
        db_path = out_dir / "warehouse.duckdb"

        con = self._connect()
        meta_tables = {}
        try:
            frames = {}
            for name, sql in mapping.items():
                t0 = time.perf_counter()
                df = self._read(con, sql)
                frames[name] = df
                meta_tables[name] = {
                    "rows": int(len(df)), "seconds": round(time.perf_counter() - t0, 2), "query": " ".join(sql.split()),
                    "sha256": hashlib.sha256(pd.util.hash_pandas_object(df, index=False).values.tobytes()).hexdigest()[:16],
                }
        finally:
            con.rollback()  # aucune transaction n'est jamais validée sur la source
            con.close()

        duck = duckdb.connect(str(db_path))
        try:
            for name, df in frames.items():
                for col in df.columns:
                    if df[col].dtype == object and df[col].map(lambda v: hasattr(v, "as_tuple")).any():
                        df[col] = pd.to_numeric(df[col])  # Decimal → float
                duck.register("_df", df)
                duck.execute(f'CREATE TABLE "{name}" AS SELECT * FROM _df')
                duck.unregister("_df")
        finally:
            duck.close()
        meta = {"source": "sqlserver", "server": self.settings.server, "database": self.settings.database,
                "extracted_at": stamp, "tables": meta_tables}
        (out_dir / "source.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return db_path


def snapshot_metadata(db_path: str | Path) -> dict | None:
    path = Path(db_path).parent / "source.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
