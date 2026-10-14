"""Façade applicative partagée par l'API FastAPI et l'interface Gradio."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from .agents import REVIEW_NODE, build_graph
from .audit import AuditLog
from .config import Settings, get_settings
from .connectors import SqlServerSource, snapshot_metadata
from .detection import CHECKS, make_context
from .generator import SCENARIOS, build_lab
from .profiling import profile_warehouse
from .remediation import Verifier, validate_sandbox_sql
from .report import to_html, to_json
from .warehouse import Warehouse, load_csv_workspace


def _default_checkpointer(settings: Settings):
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        return SqliteSaver(sqlite3.connect(settings.checkpoint_db, check_same_thread=False))
    except ImportError:  # repli : état en mémoire (perdu au redémarrage)
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()


class DataSentinel:
    def __init__(self, settings: Settings | None = None, checkpointer: Any = None):
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self.audit = AuditLog(self.settings.audit_db)
        self.graph = build_graph(self.settings, self.audit, checkpointer or _default_checkpointer(self.settings))

    # ---------------------------------------------------------------- données
    def create_lab(self, scenario: str = "all") -> Path:
        if scenario not in SCENARIOS:
            raise ValueError(f"Scénario inconnu : {scenario}")
        return build_lab(scenario, self.settings.labs_dir)

    def import_csv(self, files: dict[str, bytes], sep: str | None = ",") -> Path:
        ws = self.settings.home / "workspaces" / uuid.uuid4().hex[:10]
        return load_csv_workspace(files, ws / "warehouse.duckdb", sep=sep)

    def sqlserver_test(self) -> dict:
        return SqlServerSource(self.settings.sqlserver).test_connection()

    def sqlserver_extract(self, actor: str = "analyste") -> Path:
        """Instantané en lecture seule de la base SQL Server configurée (jamais modifiée)."""
        db = SqlServerSource(self.settings.sqlserver).extract(self.settings.sources_dir)
        meta = snapshot_metadata(db) or {}
        self.audit.record("source", actor, "sqlserver_extract",
                          {"db_path": str(db), "server": meta.get("server"), "database": meta.get("database"),
                           "tables": {k: {kk: vv for kk, vv in v.items() if kk != "query"} for k, v in meta.get("tables", {}).items()}})
        return db

    def llm_check(self) -> dict:
        """Vérifie la configuration Claude par un appel minimal (aucune donnée métier envoyée)."""
        s = self.settings
        if s.llm_provider != "anthropic":
            return {"ok": False, "message": "Rédaction locale active (DATASENTINEL_LLM_PROVIDER=none)."}
        try:
            import anthropic

            client = anthropic.Anthropic()
            resp = client.messages.create(model=s.llm_model, max_tokens=50, output_config={"effort": "low"},
                                          messages=[{"role": "user", "content": "Réponds uniquement : OK"}])
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            return {"ok": True, "message": f"Claude répond ({resp.model}) : {text[:40]}"}
        except Exception as exc:
            return {"ok": False, "message": f"{type(exc).__name__} : {str(exc)[:300]}"}

    def profile(self, db_path: str | Path) -> list[dict]:
        with Warehouse(db_path) as wh:
            return profile_warehouse(wh)

    # ---------------------------------------------------------------- enquêtes
    @staticmethod
    def _config(run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}}

    @staticmethod
    def new_run_id() -> str:
        return "run_" + uuid.uuid4().hex[:12]

    def start(self, db_path: str | Path, actor: str = "analyste", as_of: str | None = None,
              checks: list[str] | None = None, run_id: str | None = None) -> dict:
        """checks : familles de contrôles à appliquer (clés de CHECKS) ; None = toutes.
        run_id : fourni à l'avance pour suivre la progression d'une enquête lancée en arrière-plan."""
        unknown = set(checks or []) - set(CHECKS)
        if unknown:
            raise ValueError(f"Contrôles inconnus : {sorted(unknown)}")
        if checks is not None and not checks:
            raise ValueError("Sélectionnez au moins un contrôle.")
        run_id = run_id or self.new_run_id()
        self.audit.record(run_id, actor, "run_started", {"db_path": str(db_path), "as_of": as_of, "checks": checks,
                                                         "llm_provider": self.settings.llm_provider,
                                                         "llm_model": self.settings.llm_model})
        self.graph.invoke({"run_id": run_id, "db_path": str(Path(db_path).resolve()), "as_of": as_of,
                           "checks": checks, "trace": [], "queries": [], "review_log": []}, self._config(run_id))
        return self.get(run_id)

    def get(self, run_id: str) -> dict:
        snap = self.graph.get_state(self._config(run_id))
        if not snap.values:
            raise KeyError(run_id)
        state = dict(snap.values)
        state["awaiting_review"] = REVIEW_NODE in (snap.next or ())
        state["status"] = "awaiting_review" if state["awaiting_review"] else ("completed" if "report" in state else "running")
        return state

    def review(self, run_id: str, decisions: dict[str, dict], reviewer: str) -> dict:
        """decisions : {plan_id: {"decision": "approve"|"reject"|"modify", "sql": ..., "comment": ...}}"""
        state = self.get(run_id)
        if not state["awaiting_review"]:
            raise RuntimeError("Cette enquête n'attend pas de validation.")
        if self.annotations(run_id)["paused"]:
            raise RuntimeError("Enquête en pause : reprenez-la avant de valider.")
        known = {p["id"] for p in state["plans"]}
        unknown = set(decisions) - known
        if unknown:
            raise ValueError(f"Plans inconnus : {sorted(unknown)}")
        for d in decisions.values():
            if d.get("decision") not in ("approve", "reject", "modify"):
                raise ValueError("Décision attendue : approve, reject ou modify.")
        self.graph.update_state(self._config(run_id), {"review": {"reviewer": reviewer, "decisions": decisions}})
        self.graph.invoke(None, self._config(run_id))
        return self.get(run_id)

    # ---------------------------------------------------------------- actions de l'analyste
    # Toutes passent par le journal d'audit ; aucune ne modifie la source ni le statut calculé des causes.
    def annotations(self, run_id: str) -> dict:
        """Hypothèses ajoutées, hypothèses écartées et état de pause, reconstitués depuis le journal."""
        hyps: dict[str, dict] = {}
        dismissed: dict[str, dict] = {}
        paused = False
        for e in self.audit.events(run_id):
            kind, p = e["kind"], e["payload"]
            if kind == "analyst_hypothesis":
                hyps[p["id"]] = {**p, "author": e["actor"], "ts": e["ts"], "tested": None}
            elif kind == "analyst_hypothesis_tested" and p.get("id") in hyps:
                hyps[p["id"]]["tested"] = {"rows": p.get("rows"), "error": p.get("error"), "ts": e["ts"]}
            elif kind == "hypothesis_dismissed":
                dismissed[p["key"]] = {"reason": p["reason"], "by": e["actor"], "ts": e["ts"]}
            elif kind in ("run_paused", "run_resumed"):
                paused = kind == "run_paused"
        return {"hypotheses": list(hyps.values()), "dismissed": dismissed, "paused": paused}

    def set_paused(self, run_id: str, paused: bool, actor: str) -> None:
        self.get(run_id)
        self.audit.record(run_id, actor, "run_paused" if paused else "run_resumed", {})

    def add_hypothesis(self, run_id: str, case_id: str, statement: str, sql: str, actor: str,
                       about: str | None = None) -> dict:
        """about : clé d'une hypothèse des agents que l'analyste teste avec sa propre requête."""
        state = self.get(run_id)
        if case_id not in {c["id"] for c in state.get("cases", [])}:
            raise ValueError(f"Dossier inconnu : {case_id}")
        if not statement.strip():
            raise ValueError("Énoncé de l'hypothèse requis.")
        hyp = {"id": "hyp_" + uuid.uuid4().hex[:8], "case_id": case_id, "statement": statement.strip(),
               "sql": sql.strip(), "about": about}
        self.audit.record(run_id, actor, "analyst_hypothesis", hyp)
        return hyp

    def dismiss_hypothesis(self, run_id: str, key: str, reason: str, actor: str) -> None:
        if not reason.strip():
            raise ValueError("Indiquez pourquoi l'hypothèse est écartée.")
        self.get(run_id)
        self.audit.record(run_id, actor, "hypothesis_dismissed", {"key": key, "reason": reason.strip()})

    def rerun_query(self, run_id: str, sql: str, actor: str, hypothesis_id: str | None = None) -> pd.DataFrame:
        """Relance en lecture seule une requête de preuve (ou celle d'une hypothèse de l'analyste)."""
        state = self.get(run_id)
        if "?" in sql:
            raise ValueError("Requête paramétrée : ses paramètres ne sont pas conservés, relancez l'analyse complète.")
        df = pd.DataFrame()
        try:
            with Warehouse(state["db_path"]) as wh:
                for stmt in [x.strip() for x in sql.split(";") if x.strip()]:
                    df = wh.query(stmt)
        except Exception as exc:
            if hypothesis_id:
                self.audit.record(run_id, actor, "analyst_hypothesis_tested", {"id": hypothesis_id, "error": str(exc)})
            raise
        if hypothesis_id:
            self.audit.record(run_id, actor, "analyst_hypothesis_tested", {"id": hypothesis_id, "rows": len(df)})
        else:
            self.audit.record(run_id, actor, "query_rerun", {"sql": " ".join(sql.split()), "rows": len(df)})
        return df

    def test_on_copy(self, run_id: str, plan_id: str, sql: str, actor: str) -> dict:
        """Teste une version (éventuellement modifiée) d'un plan sur une copie, sans enregistrer de décision."""
        state = self.get(run_id)
        plan = next((p for p in state.get("plans", []) if p["id"] == plan_id), None)
        if plan is None:
            raise ValueError(f"Plan inconnu : {plan_id}")
        validate_sandbox_sql(sql)
        rc = next(r for r in state["root_causes"] if r["id"] == plan["root_cause_id"])
        as_of = date.fromisoformat(state["as_of"]) if state.get("as_of") else None
        with Warehouse(state["db_path"]) as wh:
            result = Verifier(wh, make_context(wh, self.settings, as_of)).test_plan(
                rc, {**plan, "sql": sql, "executable_in_sandbox": bool(sql.strip())})
        self.audit.record(run_id, actor, "sandbox_test", {"plan_id": plan_id, "sql": sql, "executed": result["executed"],
                                                          "passed": result["passed"]})
        return result

    def report(self, run_id: str) -> dict:
        state = self.get(run_id)
        if "report" not in state:
            raise RuntimeError("Rapport disponible après la validation humaine.")
        return state["report"]

    def report_json(self, run_id: str) -> str:
        self.audit.record(run_id, "export", "report_exported", {"format": "json"})
        return to_json(self.report(run_id))

    def report_html(self, run_id: str) -> str:
        self.audit.record(run_id, "export", "report_exported", {"format": "html"})
        return to_html(self.report(run_id))

    def approved_script(self, run_id: str) -> str:
        """Script SQL des plans approuvés, à exécuter par l'équipe data (jamais exécuté par l'application)."""
        state = self.get(run_id)
        lines = [f"-- DataSentinel AI — {run_id} — plans approuvés",
                 "-- À exécuter dans une transaction après sauvegarde des tables ciblées.", ""]
        for p in state.get("plans", []):
            if p.get("decision") == "approved" and p.get("sql"):
                lines += [f"-- {p['title']} (approuvé par {p.get('reviewed_by')})", "BEGIN TRANSACTION;", p["sql"], "COMMIT;", ""]
        return "\n".join(lines)

    def runs(self) -> list[dict]:
        return [{"run_id": e["run_id"], "ts": e["ts"], "actor": e["actor"], **e["payload"]}
                for e in self.audit.events(kind="run_started")][::-1]
