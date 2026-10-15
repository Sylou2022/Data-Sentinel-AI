"""API REST et interface web DataSentinel AI (FastAPI). Lancer : python -m api  (http://127.0.0.1:8000)"""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from datasentinel import __version__
from datasentinel.evaluation import evaluate
from datasentinel.generator import SCENARIOS
from datasentinel.service import DataSentinel

from .deps import get_service
from .ui import WEB_DIR, router as ui_router

app = FastAPI(title="DataSentinel AI", version=__version__,
              description="De l'anomalie à la résolution : détecter, enquêter, expliquer, proposer, vérifier.")
@app.middleware("http")
async def no_stale_ui(request, call_next):
    """L'interface est toujours revalidée : une mise à jour est visible sans vider le cache du navigateur."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.include_router(ui_router)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


LOGO = Path(__file__).resolve().parent.parent / "app" / "image" / "Logo DataSentinel AI _ bouclier analytique.png"


@app.get("/assets/logo.png", include_in_schema=False)
def logo():
    return FileResponse(LOGO, media_type="image/png")


@app.get("/", include_in_schema=False)
def index():
    """Interface web (écrans Vue d'ensemble, Import, Enquêtes, Rapports)."""
    return FileResponse(WEB_DIR / "index.html")


class StartRequest(BaseModel):
    db_path: str | None = None
    scenario: str | None = Field(None, description=f"Génère un laboratoire : {SCENARIOS}")
    as_of: str | None = None
    actor: str = "analyste"


class Decision(BaseModel):
    decision: str = Field(..., pattern="^(approve|reject|modify)$")
    sql: str | None = None
    comment: str | None = None


class ReviewRequest(BaseModel):
    reviewer: str
    decisions: dict[str, Decision]


def _public(state: dict) -> dict:
    keys = ("run_id", "status", "awaiting_review", "as_of", "trace", "root_causes", "unexplained", "plans",
            "residual", "review_log")
    out = {k: state.get(k) for k in keys}
    out["anomalies"] = state.get("detection", {}).get("anomalies", [])
    return out


def _db(path: str, svc: DataSentinel) -> Path:
    """Seules les bases créées par l'application (sous DATASENTINEL_HOME) sont accessibles."""
    p = Path(path).resolve()
    if not p.is_relative_to(svc.settings.home.resolve()) or p.suffix != ".duckdb" or not p.exists():
        raise HTTPException(404, "Base DuckDB introuvable dans l'espace de travail.")
    return p


@app.get("/health")
def health(svc: DataSentinel = Depends(get_service)):
    return {"status": "ok", "version": __version__, "llm_provider": svc.settings.llm_provider}


@app.post("/datasets/lab/{scenario}")
def create_lab(scenario: str, svc: DataSentinel = Depends(get_service)):
    if scenario not in SCENARIOS:
        raise HTTPException(400, f"Scénario inconnu. Choix : {SCENARIOS}")
    return {"db_path": str(svc.create_lab(scenario))}


@app.get("/datasets/sqlserver/test")
def sqlserver_test(svc: DataSentinel = Depends(get_service)):
    try:
        return svc.sqlserver_test()
    except Exception as exc:
        raise HTTPException(502, f"Connexion SQL Server impossible : {type(exc).__name__}")


@app.post("/datasets/sqlserver/extract")
def sqlserver_extract(svc: DataSentinel = Depends(get_service)):
    try:
        return {"db_path": str(svc.sqlserver_extract(actor="api"))}
    except PermissionError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Extraction SQL Server impossible : {type(exc).__name__}")


@app.post("/datasets/upload")
async def upload(files: list[UploadFile] = File(...), svc: DataSentinel = Depends(get_service)):
    content = {Path(f.filename or "table.csv").name: await f.read() for f in files}
    try:
        return {"db_path": str(svc.import_csv(content))}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/datasets/profile")
def profile(db_path: str, svc: DataSentinel = Depends(get_service)):
    return svc.profile(_db(db_path, svc))


@app.post("/investigations")
def start(req: StartRequest, svc: DataSentinel = Depends(get_service)):
    if req.scenario:
        db = svc.create_lab(req.scenario)
    elif req.db_path:
        db = _db(req.db_path, svc)
    else:
        raise HTTPException(400, "Fournir db_path ou scenario.")
    return _public(svc.start(db, req.actor, req.as_of))


@app.get("/investigations")
def list_runs(svc: DataSentinel = Depends(get_service)):
    return svc.runs()


@app.get("/investigations/{run_id}")
def get_run(run_id: str, svc: DataSentinel = Depends(get_service)):
    try:
        return _public(svc.get(run_id))
    except KeyError:
        raise HTTPException(404, "Enquête inconnue.")


@app.post("/investigations/{run_id}/review")
def review(run_id: str, req: ReviewRequest, svc: DataSentinel = Depends(get_service)):
    try:
        state = svc.review(run_id, {k: v.model_dump() for k, v in req.decisions.items()}, req.reviewer)
    except KeyError:
        raise HTTPException(404, "Enquête inconnue.")
    except (RuntimeError, ValueError, PermissionError) as exc:
        raise HTTPException(409, str(exc))
    return _public(state)


@app.get("/investigations/{run_id}/report.json")
def report_json(run_id: str, svc: DataSentinel = Depends(get_service)):
    try:
        return Response(svc.report_json(run_id), media_type="application/json")
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(409, str(exc))


@app.get("/investigations/{run_id}/report.html", response_class=HTMLResponse)
def report_html(run_id: str, svc: DataSentinel = Depends(get_service)):
    try:
        return svc.report_html(run_id)
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(409, str(exc))


@app.get("/investigations/{run_id}/approved.sql", response_class=PlainTextResponse)
def approved_sql(run_id: str, svc: DataSentinel = Depends(get_service)):
    return svc.approved_script(run_id)


@app.get("/investigations/{run_id}/audit")
def audit(run_id: str, svc: DataSentinel = Depends(get_service)):
    return [e for e in svc.audit.events(run_id) if e["kind"] != "query"]


@app.get("/evaluation")
def run_evaluation():
    return evaluate()
