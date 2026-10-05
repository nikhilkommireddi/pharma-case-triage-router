"""HTTP API for the triage & routing worker."""

from __future__ import annotations

from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

load_dotenv()

from . import db, pipeline  # noqa: E402
from .extraction import ClaudeExtractor  # noqa: E402
from .models import CaseStatus, Destination, IntakeCase, TriageResult  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not hasattr(app.state, "conn"):
        app.state.conn = db.connect()
    if not hasattr(app.state, "extractor"):
        app.state.extractor = ClaudeExtractor()
    yield


app = FastAPI(title="Case Triage & Routing", lifespan=lifespan)


class ReviewRequest(BaseModel):
    reviewer: str
    approve: bool
    destinations: list[Destination] | None = None
    comment: str


@app.post("/cases", response_model=TriageResult, status_code=201)
def submit_case(case: IntakeCase):
    try:
        return pipeline.triage(app.state.conn, case, app.state.extractor)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/cases")
def list_cases(status: CaseStatus | None = None):
    return db.list_cases(app.state.conn, status.value if status else None)


@app.get("/cases/{case_id}")
def get_case(case_id: str):
    stored = db.get_case(app.state.conn, case_id)
    if not stored:
        raise HTTPException(404, "Case not found")
    stored["records"] = db.records_for_case(app.state.conn, case_id)
    return stored


@app.get("/cases/{case_id}/audit")
def case_audit(case_id: str):
    return db.audit_trail(app.state.conn, case_id)


@app.post("/cases/{case_id}/review", response_model=TriageResult)
def review_case(case_id: str, req: ReviewRequest):
    try:
        return pipeline.review(app.state.conn, case_id, req.reviewer, req.approve, req.destinations, req.comment)
    except KeyError:
        raise HTTPException(404, "Case not found")
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/queues/human-triage")
def human_triage_queue():
    """Cases awaiting a triage lead, earliest regulatory deadline first."""
    conn = app.state.conn
    return (db.list_cases(conn, CaseStatus.pending_review.value)
            + db.list_cases(conn, CaseStatus.failed.value))


@app.get("/queues/{destination}")
def destination_queue(destination: Destination):
    return db.records_for_destination(app.state.conn, destination.value)


@app.get("/audit/verify")
def verify_audit():
    ok, bad_seq = db.verify_audit_chain(app.state.conn)
    return {"intact": ok, "first_broken_entry": bad_seq}
