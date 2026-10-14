"""Journal d'audit SQLite : requêtes, versions de modèles, décisions humaines, exports."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    run_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
"""


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as con:
            con.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def record(self, run_id: str, actor: str, kind: str, payload: dict | None = None) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT INTO events (ts, run_id, actor, kind, payload) VALUES (?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), run_id, actor, kind,
                 json.dumps(payload or {}, ensure_ascii=False, default=str)),
            )

    def events(self, run_id: str | None = None, kind: str | None = None) -> list[dict]:
        sql = "SELECT ts, run_id, actor, kind, payload FROM events"
        where, args = [], []
        if run_id:
            where.append("run_id = ?")
            args.append(run_id)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._connect() as con:
            rows = con.execute(sql + " ORDER BY id", args).fetchall()
        return [{"ts": r[0], "run_id": r[1], "actor": r[2], "kind": r[3], "payload": json.loads(r[4])} for r in rows]
