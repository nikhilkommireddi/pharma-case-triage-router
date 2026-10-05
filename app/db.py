"""Persistence: cases, downstream records, and a hash-chained audit log.

Runs on Postgres (Railway, docker-compose) or SQLite (tests, quick local runs),
selected by ``DATABASE_URL``.

The audit log is append-only — enforced by a database trigger — and each
entry's hash covers the previous entry's hash, so any edit or deletion that
bypasses the trigger breaks the chain and is caught by ``verify_audit_chain``.
That's the tamper-evidence piece of a Part 11 audit trail; access control and
e-signatures are out of scope here.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    Engine,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.pool import StaticPool

metadata = MetaData()

cases = Table(
    "cases", metadata,
    Column("case_id", String(64), primary_key=True),
    Column("intake_json", Text, nullable=False),
    Column("extraction_json", Text),
    Column("result_json", Text),
    Column("status", String(32), nullable=False, index=True),
    Column("due_date", String(10)),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
)

external_records = Table(
    "external_records", metadata,
    Column("record_id", String(96), primary_key=True),
    Column("system", String(64), nullable=False),
    Column("destination", String(32), nullable=False, index=True),
    Column("case_id", String(64), ForeignKey("cases.case_id"), nullable=False, index=True),
    Column("payload_json", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
)

audit_log = Table(
    "audit_log", metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("ts", String(40), nullable=False),
    Column("case_id", String(64), index=True),
    Column("actor", String(128), nullable=False),
    Column("action", String(64), nullable=False),
    Column("detail_json", Text, nullable=False),
    Column("prev_hash", String(64), nullable=False),
    Column("hash", String(64), nullable=False),
)

_TRIGGERS = {
    "sqlite": [
        """CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
           BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END""",
        """CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
           BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END""",
    ],
    "postgresql": [
        """CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS $$
           BEGIN RAISE EXCEPTION 'audit_log is append-only'; END; $$ LANGUAGE plpgsql""",
        """CREATE OR REPLACE TRIGGER audit_no_modify BEFORE UPDATE OR DELETE ON audit_log
           FOR EACH ROW EXECUTE FUNCTION audit_log_immutable()""",
    ],
}

GENESIS = "0" * 64
_append_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _normalize_url(url: str) -> str:
    # Railway and most hosts hand out postgres:// or postgresql:// URLs; pin the psycopg 3 driver.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def connect(url: str | None = None) -> Engine:
    url = url or os.getenv("DATABASE_URL") or "sqlite:///triage.db"
    if url == ":memory:":
        url = "sqlite://"
    url = _normalize_url(url)
    if url.startswith("sqlite"):
        kw = {"connect_args": {"check_same_thread": False}}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kw["poolclass"] = StaticPool
        engine = create_engine(url, **kw)
    else:
        engine = create_engine(url, pool_pre_ping=True)
    metadata.create_all(engine)
    with engine.begin() as c:
        for stmt in _TRIGGERS.get(engine.dialect.name, []):
            c.exec_driver_sql(stmt)
    return engine


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def _entry_hash(prev_hash: str, ts: str, case_id: str | None, actor: str, action: str, detail_json: str) -> str:
    material = "|".join([prev_hash, ts, case_id or "", actor, action, detail_json])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def audit(engine: Engine, case_id: str | None, actor: str, action: str, detail: dict) -> None:
    detail_json = json.dumps(detail, sort_keys=True, default=str)
    # Reading the last hash and appending must be atomic or two writers fork
    # the chain: a process lock covers threads, a table lock covers replicas.
    with _append_lock, engine.begin() as c:
        if engine.dialect.name == "postgresql":
            c.execute(text("LOCK TABLE audit_log IN SHARE ROW EXCLUSIVE MODE"))
        prev = c.execute(select(audit_log.c.hash).order_by(audit_log.c.seq.desc()).limit(1)).scalar() or GENESIS
        ts = _now()
        c.execute(insert(audit_log).values(
            ts=ts, case_id=case_id, actor=actor, action=action, detail_json=detail_json,
            prev_hash=prev, hash=_entry_hash(prev, ts, case_id, actor, action, detail_json),
        ))


def audit_trail(engine: Engine, case_id: str) -> list[dict]:
    with engine.connect() as c:
        rows = c.execute(select(audit_log).where(audit_log.c.case_id == case_id).order_by(audit_log.c.seq)).mappings()
        return [{**dict(r), "detail": json.loads(r["detail_json"])} for r in rows]


def verify_audit_chain(engine: Engine) -> tuple[bool, int | None]:
    """Returns (ok, first_bad_seq)."""
    prev = GENESIS
    with engine.connect() as c:
        for r in c.execute(select(audit_log).order_by(audit_log.c.seq)).mappings():
            expected = _entry_hash(prev, r["ts"], r["case_id"], r["actor"], r["action"], r["detail_json"])
            if r["prev_hash"] != prev or r["hash"] != expected:
                return False, r["seq"]
            prev = r["hash"]
    return True, None


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

def save_case(engine: Engine, case_id: str, intake_json: str, status: str,
              extraction_json: str | None = None, result_json: str | None = None,
              due_date: str | None = None) -> None:
    now = _now()
    with engine.begin() as c:
        exists = c.execute(select(cases.c.case_id).where(cases.c.case_id == case_id)).first()
        if exists is None:
            c.execute(insert(cases).values(
                case_id=case_id, intake_json=intake_json, extraction_json=extraction_json,
                result_json=result_json, status=status, due_date=due_date, created_at=now, updated_at=now,
            ))
            return
        values = {"status": status, "updated_at": now}
        if extraction_json is not None:
            values["extraction_json"] = extraction_json
        if result_json is not None:
            values["result_json"] = result_json
        if due_date is not None:
            values["due_date"] = due_date
        c.execute(update(cases).where(cases.c.case_id == case_id).values(**values))


def _case_row(r) -> dict:
    return {
        "case_id": r["case_id"],
        "status": r["status"],
        "due_date": r["due_date"],
        "intake": json.loads(r["intake_json"]),
        "extraction": json.loads(r["extraction_json"]) if r["extraction_json"] else None,
        "result": json.loads(r["result_json"]) if r["result_json"] else None,
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def get_case(engine: Engine, case_id: str) -> dict | None:
    with engine.connect() as c:
        r = c.execute(select(cases).where(cases.c.case_id == case_id)).mappings().first()
        return _case_row(r) if r else None


def list_cases(engine: Engine, status: str | None = None) -> list[dict]:
    """Summaries for queue views, earliest regulatory deadline first."""
    q = select(cases)
    if status:
        q = q.where(cases.c.status == status)
    q = q.order_by(cases.c.due_date.is_(None), cases.c.due_date, cases.c.created_at)
    out = []
    with engine.connect() as c:
        for r in c.execute(q).mappings():
            intake = json.loads(r["intake_json"])
            result = json.loads(r["result_json"]) if r["result_json"] else {}
            out.append({
                "case_id": r["case_id"],
                "status": r["status"],
                "due_date": r["due_date"],
                "created_at": r["created_at"],
                "channel": intake.get("channel"),
                "received_at": intake.get("received_at"),
                "intents": result.get("intents", []),
                "destinations": [rt["destination"] for rt in result.get("routes", [])],
                "confidence": result.get("confidence"),
                "clock_category": (result.get("clock") or {}).get("category"),
                "review_reasons": result.get("review_reasons", []),
            })
    return out


# ---------------------------------------------------------------------------
# Downstream records
# ---------------------------------------------------------------------------

def insert_record(engine: Engine, record_id: str, system: str, destination: str, case_id: str, payload: dict) -> None:
    with engine.begin() as c:
        c.execute(insert(external_records).values(
            record_id=record_id, system=system, destination=destination, case_id=case_id,
            payload_json=json.dumps(payload, default=str), created_at=_now(),
        ))


def update_record_payload(engine: Engine, record_id: str, payload: dict) -> None:
    with engine.begin() as c:
        c.execute(update(external_records).where(external_records.c.record_id == record_id)
                  .values(payload_json=json.dumps(payload, default=str)))


def _record_row(r) -> dict:
    d = {k: v for k, v in r.items() if k != "payload_json"}
    d["payload"] = json.loads(r["payload_json"])
    return d


def records_for_case(engine: Engine, case_id: str) -> list[dict]:
    with engine.connect() as c:
        q = select(external_records).where(external_records.c.case_id == case_id).order_by(external_records.c.record_id)
        return [_record_row(r) for r in c.execute(q).mappings()]


def records_for_destination(engine: Engine, destination: str) -> list[dict]:
    with engine.connect() as c:
        q = (select(external_records).where(external_records.c.destination == destination)
             .order_by(external_records.c.created_at.desc()))
        return [_record_row(r) for r in c.execute(q).mappings()]
