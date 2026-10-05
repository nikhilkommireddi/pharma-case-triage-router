"""Downstream system connectors (mocked) and cross-system case forking.

Each connector builds a payload in the target system's terms and writes it to
the local ``external_records`` table, standing in for Argus / TrackWise /
Service Cloud APIs. Swapping in a real integration means implementing
``Connector.create`` against that system — nothing upstream changes.

Forking: one inbound case becomes one child record per destination. Every
child carries the parent case ID and the IDs of its siblings, so PV can see
the linked quality complaint and vice versa without anyone re-keying data.
"""

from __future__ import annotations

from sqlalchemy import Engine
from itertools import count

from . import db
from .models import CaseExtraction, Destination, IntakeCase, Route, TriageResult


class Connector:
    system: str
    prefix: str
    destination: Destination

    def build_payload(self, case: IntakeCase, x: CaseExtraction, result: TriageResult, route: Route) -> dict:
        raise NotImplementedError

    def create(self, conn: Engine, record_id: str, case: IntakeCase, payload: dict) -> str:
        db.insert_record(conn, record_id, self.system, self.destination.value, case.case_id, payload)
        return record_id


class SafetyConnector(Connector):
    system = "Argus Safety (mock)"
    prefix = "AER"
    destination = Destination.drug_safety

    def build_payload(self, case, x, result, route):
        clock = result.clock
        return {
            "record_type": "ICSR" if result.valid_icsr else "Potential AE — follow-up",
            "initial_receipt_date": clock.day_zero.isoformat() if clock else None,
            "report_category": clock.category if clock else None,
            "regulatory_due_date": clock.due_date.isoformat() if clock and clock.due_date else None,
            "serious": clock.serious if clock else None,
            "listedness": {True: "listed", False: "unlisted", None: "unassessed"}[clock.expected] if clock else None,
            "patient": x.patient.identifiers,
            "reporter": {"identifiers": x.reporter.identifiers, "is_hcp": x.reporter_is_hcp,
                         "channel": case.channel.value, "country": case.country},
            "suspect_products": [{"name": p.name, "lot": p.lot_number} for p in x.products],
            "events": [{"term": e.term, "verbatim": e.evidence,
                        "seriousness": [s.criterion.value for s in e.seriousness]} for e in x.adverse_events],
            "onset_date": x.event_onset_date.isoformat() if x.event_onset_date else None,
            "follow_up_checklist": route.missing_fields,
        }


class QualityConnector(Connector):
    system = "TrackWise (mock)"
    prefix = "PQC"
    destination = Destination.product_quality

    def build_payload(self, case, x, result, route):
        return {
            "record_type": "Product Complaint",
            "aware_date": case.received_at.date().isoformat(),
            "products": [{"name": p.name, "lot": p.lot_number} for p in x.products],
            "defect_description": x.complaint_description,
            "sample_available": x.sample_available,
            "associated_adverse_event": bool(x.adverse_events),
            "follow_up_checklist": route.missing_fields,
        }


class MedInfoConnector(Connector):
    system = "Service Cloud (mock)"
    prefix = "MI"
    destination = Destination.medical_information

    def build_payload(self, case, x, result, route):
        return {
            "record_type": "Medical Inquiry",
            "question": x.medical_question,
            "products": [p.name for p in x.products],
            "requester_is_hcp": x.reporter_is_hcp,
            "response_channel": case.channel.value,
            "follow_up_checklist": route.missing_fields,
        }


CONNECTORS: dict[Destination, Connector] = {
    c.destination: c for c in (SafetyConnector(), QualityConnector(), MedInfoConnector())
}


def dispatch(conn: Engine, case: IntakeCase, x: CaseExtraction, result: TriageResult, actor: str) -> TriageResult:
    """Create one linked child record per routable destination; returns the updated result."""
    targets = [r for r in result.routes if r.destination in CONNECTORS]
    seq = count(1)
    created: list[tuple[Route, Connector, str, dict]] = []

    for route in targets:
        conn_ = CONNECTORS[route.destination]
        record_id = f"{conn_.prefix}-{case.case_id}-{next(seq):02d}"
        payload = conn_.build_payload(case, x, result, route)
        payload["parent_case_id"] = case.case_id
        conn_.create(conn, record_id, case, payload)
        route.external_system, route.external_id = conn_.system, record_id
        created.append((route, conn_, record_id, payload))
        db.audit(conn, case.case_id, actor, "RECORD_CREATED",
                 {"record_id": record_id, "system": conn_.system, "destination": route.destination.value})

    if len(created) > 1:
        all_ids = [rid for _, _, rid, _ in created]
        for _, _, rid, payload in created:
            payload["linked_records"] = [i for i in all_ids if i != rid]
            db.update_record_payload(conn, rid, payload)
        db.audit(conn, case.case_id, actor, "CASE_FORKED", {"linked_records": all_ids})

    return result
