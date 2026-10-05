import pytest
from fastapi.testclient import TestClient

from app import db, pipeline
from app.main import app
from app.models import CaseStatus, Destination

from conftest import FakeExtractor, make_case, pen_extraction


def test_auto_routed_case_creates_linked_child_records(conn):
    result = pipeline.triage(conn, make_case(), FakeExtractor(pen_extraction()))
    assert result.status == CaseStatus.auto_routed

    records = db.records_for_case(conn, "C-001")
    assert {r["destination"] for r in records} == {"drug_safety", "product_quality"}
    ids = {r["record_id"] for r in records}
    for r in records:
        assert r["payload"]["parent_case_id"] == "C-001"
        assert set(r["payload"]["linked_records"]) == ids - {r["record_id"]}

    actions = [e["action"] for e in db.audit_trail(conn, "C-001")]
    assert actions[0] == "CASE_RECEIVED"
    assert actions.count("RULE_EVALUATED") == 4
    assert "CASE_FORKED" in actions
    assert db.verify_audit_chain(conn) == (True, None)


def test_extraction_failure_goes_to_human_not_dropped(conn):
    result = pipeline.triage(conn, make_case(), FakeExtractor(error="timeout"))
    assert result.status == CaseStatus.failed
    assert result.routes[0].destination == Destination.human_triage
    assert db.records_for_case(conn, "C-001") == []


def test_duplicate_case_rejected(conn):
    pipeline.triage(conn, make_case(), FakeExtractor(pen_extraction()))
    with pytest.raises(ValueError):
        pipeline.triage(conn, make_case(), FakeExtractor(pen_extraction()))


def test_review_approve_dispatches_proposed_routes(conn):
    x = pen_extraction(ambiguities=["Unclear whether pen or user error"])
    result = pipeline.triage(conn, make_case(), FakeExtractor(x))
    assert result.status == CaseStatus.pending_review
    assert db.records_for_case(conn, "C-001") == []

    with pytest.raises(ValueError):
        pipeline.review(conn, "C-001", "lead1", True, None, "  ")

    reviewed = pipeline.review(conn, "C-001", "lead1", True, None, "Confirmed device failure")
    assert reviewed.status == CaseStatus.review_approved
    assert len(db.records_for_case(conn, "C-001")) == 2
    signoff = [e for e in db.audit_trail(conn, "C-001") if e["action"] == "REVIEW_SIGNED_OFF"][0]
    assert signoff["actor"] == "user:lead1"


def test_review_override_changes_destinations(conn):
    pipeline.triage(conn, make_case(), FakeExtractor(error="bad output"))
    reviewed = pipeline.review(conn, "C-001", "lead1", False, [Destination.medical_information], "Just a dosing question")
    assert [r.destination for r in reviewed.routes] == [Destination.medical_information]


def test_audit_log_is_append_only_and_tamper_evident(conn):
    pipeline.triage(conn, make_case(), FakeExtractor(pen_extraction()))
    with pytest.raises(Exception):
        with conn.begin() as c:
            c.exec_driver_sql("UPDATE audit_log SET actor = 'x' WHERE seq = 1")
    # Simulate someone with admin rights bypassing the trigger.
    with conn.begin() as c:
        c.exec_driver_sql("DROP TRIGGER " + ("audit_no_update" if conn.dialect.name == "sqlite"
                                             else "audit_no_modify ON audit_log"))
        first = c.exec_driver_sql("SELECT MIN(seq) FROM audit_log").scalar()
        c.exec_driver_sql(f"UPDATE audit_log SET detail_json = '{{}}' WHERE seq = {first + 1}")
    assert db.verify_audit_chain(conn) == (False, first + 1)


def test_api_end_to_end(conn):
    app.state.conn = conn
    app.state.extractor = FakeExtractor(pen_extraction())
    with TestClient(app) as client:
        r = client.post("/api/cases", json=make_case().model_dump(mode="json"))
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "auto_routed"

        assert client.post("/api/cases", json=make_case().model_dump(mode="json")).status_code == 409
        assert len(client.get("/api/queues/drug_safety").json()) == 1
        assert client.get("/api/queues/human-triage").json() == []
        assert client.get("/api/audit/verify").json()["intact"] is True
        detail = client.get("/api/cases/C-001").json()
        assert len(detail["records"]) == 2

        health = client.get("/health").json()
        assert health["status"] == "ok"
        stats = client.get("/api/stats").json()
        assert stats["total"] == 1 and stats["by_destination"]["drug_safety"] == 1
        assert len(client.get("/api/samples").json()) >= 10
    del app.state.conn, app.state.extractor
