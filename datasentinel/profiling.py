"""Profilage générique d'une table : schéma, valeurs manquantes, doublons, distributions."""
from __future__ import annotations

import pandas as pd


def profile_dataframe(df: pd.DataFrame, name: str = "table", key: str | None = None) -> dict:
    columns = []
    for col in df.columns:
        s = df[col]
        info = {
            "name": col,
            "dtype": str(s.dtype),
            "null_count": int(s.isna().sum()),
            "null_rate": round(float(s.isna().mean()), 4) if len(s) else 0.0,
            "distinct": int(s.nunique(dropna=True)),
        }
        if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s):
            desc = s.describe(percentiles=[0.01, 0.5, 0.99])
            info["stats"] = {k: (None if pd.isna(v) else round(float(v), 4)) for k, v in desc.items() if k != "count"}
        elif pd.api.types.is_datetime64_any_dtype(s):
            info["stats"] = {"min": str(s.min()), "max": str(s.max())}
        else:
            top = s.value_counts(dropna=True).head(5)
            info["top_values"] = {str(k): int(v) for k, v in top.items()}
        columns.append(info)

    key = key or next((c for c in df.columns if c.endswith("_id")), None)
    return {
        "table": name,
        "rows": int(len(df)),
        "columns": columns,
        "duplicate_rows": int(df.duplicated().sum()),
        "key": key,
        "duplicate_keys": int(df[key].duplicated().sum()) if key else None,
    }


def profile_warehouse(wh) -> list[dict]:
    keys = {"orders": "order_id", "products": "product_id", "customers": "customer_id", "pipeline_logs": "run_id"}
    return [profile_dataframe(wh.query(f'SELECT * FROM "{t}"'), t, keys.get(t)) for t in wh.tables()]
