"""End-to-end triage: extract -> ground -> rules -> route -> dispatch, fully audited."""

from __future__ import annotations

import sqlite3

from . import db
from .connectors import dispatch
from .extraction import ExtractionError, Extractor
from .grounding import ground_extraction
from .models import (
    CaseExtraction,
    CaseStatus,
    Destination,
    IntakeCase,
    Route,
    TriageResult,
)
from .routing import decide

SYSTEM_ACTOR = "system:triage-router"


def _save(conn, case: IntakeCase, result: TriageResult, extraction: CaseExtraction | None) -> None:
    db.save_case(
        conn, case.case_id, case.model_dump_json(), result.status.value,
        extraction_json=extraction.model_dump_json() if extraction else None,
        result_json=result.model_dump_json(),
        due_date=result.clock.due_date.isoformat() if result.clock and result.clock.due_date else None,
    )


def triage(conn: sqlite3.Connection, case: IntakeCase, extractor: Extractor) -> TriageResult:
    if db.get_case(conn, case.case_id):
        raise ValueError(f"Case {case.case_id} already exists")

    db.save_case(conn, case.case_id, case.model_dump_json(), "received")
    db.audit(conn, case.case_id, SYSTEM_ACTOR, "CASE_RECEIVED",
             {"channel": case.channel.value, "received_at": case.received_at, "context": case.context.value})

    try:
        raw = extractor.extract(case)
    except ExtractionError as e:
        # Never drop a case: if extraction fails, a human gets it.
        result = TriageResult(
            case_id=case.case_id, status=CaseStatus.failed, intents=[], rule_results=[],
            valid_icsr=None, clock=None, confidence=0.0,
            routes=[Route(destination=Destination.human_triage, reason=f"Automated extraction failed: {e}")],
            review_reasons=[f"Automated extraction failed: {e}"],
        )
        db.audit(conn, case.case_id, SYSTEM_ACTOR, "EXTRACTION_FAILED", {"error": str(e)})
        _save(conn, case, result, None)
        return result

    db.audit(conn, case.case_id, SYSTEM_ACTOR, "FACTS_EXTRACTED", {"extraction": raw.model_dump(mode="json")})

    grounded, dropped = ground_extraction(raw, case.transcript)
    db.audit(conn, case.case_id, SYSTEM_ACTOR, "FACTS_GROUNDED", {"dropped": dropped})

    result = decide(case, grounded, dropped)
    for r in result.rule_results:
        db.audit(conn, case.case_id, SYSTEM_ACTOR, "RULE_EVALUATED", r.model_dump(mode="json"))
    if result.clock:
        db.audit(conn, case.case_id, SYSTEM_ACTOR, "REPORTING_CLOCK_SET", result.clock.model_dump(mode="json"))
    db.audit(conn, case.case_id, SYSTEM_ACTOR, "ROUTING_DECIDED", {
        "status": result.status.value,
        "intents": [i.value for i in result.intents],
        "destinations": [r.destination.value for r in result.routes],
        "confidence": result.confidence,
        "review_reasons": result.review_reasons,
    })

    if result.status == CaseStatus.auto_routed:
        dispatch(conn, case, grounded, result, SYSTEM_ACTOR)

    _save(conn, case, result, grounded)
    return result


def review(conn: sqlite3.Connection, case_id: str, reviewer: str, approve: bool,
           destinations: list[Destination] | None, comment: str) -> TriageResult:
    """Human triage lead signs off: approve the proposed routes, or override them."""
    stored = db.get_case(conn, case_id)
    if not stored:
        raise KeyError(case_id)
    if stored["status"] not in (CaseStatus.pending_review.value, CaseStatus.failed.value):
        raise ValueError(f"Case {case_id} is not awaiting review (status={stored['status']})")
    if not comment.strip():
        raise ValueError("A review comment is required")

    case = IntakeCase.model_validate(stored["intake"])
    result = TriageResult.model_validate(stored["result"])
    extraction = (CaseExtraction.model_validate(stored["extraction"]) if stored["extraction"]
                  else CaseExtraction.model_validate({"intents": [], "patient": {"identifiable": False},
                                                      "reporter": {"identifiable": False}}))

    proposed = [r for r in result.routes if r.destination != Destination.human_triage]
    if approve:
        routes = proposed
    else:
        if not destinations:
            raise ValueError("Override requires at least one destination")
        by_dest = {r.destination: r for r in proposed}
        routes = [by_dest.get(d) or Route(destination=d, reason=f"Reviewer override: {comment}")
                  for d in destinations if d != Destination.human_triage]

    result.routes = routes
    result.status = CaseStatus.review_approved
    db.audit(conn, case_id, f"user:{reviewer}", "REVIEW_SIGNED_OFF", {
        "decision": "approve" if approve else "override",
        "destinations": [r.destination.value for r in routes],
        "comment": comment,
    })
    dispatch(conn, case, extraction, result, f"user:{reviewer}")
    _save(conn, case, result, extraction)
    return result
