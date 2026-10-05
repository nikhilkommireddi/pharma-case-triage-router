"""HTTP API for the triage & routing worker.

In production the same process also serves the built React app from
``frontend/dist`` so Railway runs a single service.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

from . import db, pipeline  # noqa: E402
from .extraction import MODEL, ClaudeExtractor  # noqa: E402
from .models import CaseStatus, Destination, IntakeCase, TriageResult  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIST = ROOT / "frontend" / "dist"
DUE_SOON_DAYS = 3


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not hasattr(app.state, "conn"):
        app.state.conn = db.connect()
    if not hasattr(app.state, "extractor"):
        app.state.extractor = ClaudeExtractor()
    yield


app = FastAPI(title="Case Triage & Routing", lifespan=lifespan)

_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:5291").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["*"], allow_headers=["*"])

api = APIRouter(prefix="/api")


class ReviewRequest(BaseModel):
    reviewer: str
    approve: bool
    destinations: list[Destination] | None = None
    comment: str


@app.get("/health")
def health():
    with app.state.conn.connect() as c:
        c.exec_driver_sql("SELECT 1")
    return {"status": "ok", "database": app.state.conn.dialect.name, "model": MODEL}


@api.post("/cases", response_model=TriageResult, status_code=201)
def submit_case(case: IntakeCase):
    try:
        return pipeline.triage(app.state.conn, case, app.state.extractor)
    except ValueError as e:
        raise HTTPException(409, str(e))


@api.get("/cases")
def list_cases(status: CaseStatus | None = None):
    return db.list_cases(app.state.conn, status.value if status else None)


@api.get("/cases/{case_id}")
def get_case(case_id: str):
    stored = db.get_case(app.state.conn, case_id)
    if not stored:
        raise HTTPException(404, "Case not found")
    stored["records"] = db.records_for_case(app.state.conn, case_id)
    return stored


@api.get("/cases/{case_id}/audit")
def case_audit(case_id: str):
    return db.audit_trail(app.state.conn, case_id)


@api.post("/cases/{case_id}/review", response_model=TriageResult)
def review_case(case_id: str, req: ReviewRequest):
    try:
        return pipeline.review(app.state.conn, case_id, req.reviewer, req.approve, req.destinations, req.comment)
    except KeyError:
        raise HTTPException(404, "Case not found")
    except ValueError as e:
        raise HTTPException(409, str(e))


@api.get("/queues/human-triage")
def human_triage_queue():
    """Cases awaiting a triage lead, earliest regulatory deadline first."""
    conn = app.state.conn
    return (db.list_cases(conn, CaseStatus.pending_review.value)
            + db.list_cases(conn, CaseStatus.failed.value))


@api.get("/queues/{destination}")
def destination_queue(destination: Destination):
    return db.records_for_destination(app.state.conn, destination.value)


@api.get("/stats")
def stats():
    """Counts for the console's summary tiles."""
    all_cases = db.list_cases(app.state.conn)
    today = date.today()
    soon = (today + timedelta(days=DUE_SOON_DAYS)).isoformat()
    open_statuses = {CaseStatus.pending_review.value, CaseStatus.failed.value}
    by_status: dict[str, int] = {}
    by_destination: dict[str, int] = {d.value: 0 for d in Destination}
    for c in all_cases:
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1
        for d in set(c["destinations"]):
            by_destination[d] = by_destination.get(d, 0) + 1
    awaiting = [c for c in all_cases if c["status"] in open_statuses]
    return {
        "total": len(all_cases),
        "by_status": by_status,
        "by_destination": by_destination,
        "awaiting_review": len(awaiting),
        "expedited_due_soon": sum(1 for c in all_cases if c["due_date"] and today.isoformat() <= c["due_date"] <= soon),
        "expedited_overdue_in_review": sum(1 for c in awaiting if c["due_date"] and c["due_date"] < today.isoformat()),
        "due_soon_days": DUE_SOON_DAYS,
    }


@api.get("/samples")
def samples():
    """Synthetic intake cases the UI offers as one-click demos."""
    items = json.loads((ROOT / "data" / "eval_cases.json").read_text(encoding="utf-8"))
    return [{"label": i["expected"]["note"], "case": i["case"]} for i in items]


@api.get("/audit/verify")
def verify_audit():
    ok, bad_seq = db.verify_audit_chain(app.state.conn)
    return {"intact": ok, "first_broken_entry": bad_seq}


app.include_router(api)

# Serve the built SPA (production). Unknown non-API paths fall back to index.html
# so client-side routes survive a page refresh.
if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        file = (FRONTEND_DIST / path).resolve()
        if path and file.is_file() and FRONTEND_DIST.resolve() in file.parents:
            return FileResponse(file)
        return FileResponse(FRONTEND_DIST / "index.html")
