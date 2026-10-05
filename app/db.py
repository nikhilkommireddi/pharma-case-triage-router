"""SQLite persistence: cases, downstream records, and a hash-chained audit log.

The audit log is append-only and each entry's hash covers the previous
entry's hash, so any edit or deletion breaks the chain and is detectable by
``verify_audit_chain``. That's the tamper-evidence piece of a Part 11 audit
trail; access control and e-signatures are out of scope for this demo.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.getenv("TRIAGE_DB_PATH", Path(__file__).resolve().parent.parent / "triage.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id      TEXT PRIMARY KEY,
    intake_json  TEXT NOT NULL,
    extraction_json TEXT,
    result_json  TEXT,
    status       TEXT NOT NULL,
    due_date     TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS external_records (
    record_id    TEXT PRIMARY KEY,
    system       TEXT NOT NULL,
    destination  TEXT NOT NULL,
    case_id      TEXT NOT NULL REFERENCES cases(case_id),
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    case_id    TEXT,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""

GENESIS = "0" * 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def _entry_hash(prev_hash: str, ts: str, case_id: str | None, actor: str, action: str, detail_json: str) -> str:
    material = "|".join([prev_hash, ts, case_id or "", actor, action, detail_json])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def audit(conn: sqlite3.Connection, case_id: str | None, actor: str, action: str, detail: dict) -> None:
    row = conn.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    prev = row["hash"] if row else GENESIS
    ts = _now()
    detail_json = json.dumps(detail, sort_keys=True, default=str)
    h = _entry_hash(prev, ts, case_id, actor, action, detail_json)
    conn.execute(
        "INSERT INTO audit_log (ts, case_id, actor, action, detail_json, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
        (ts, case_id, actor, action, detail_json, prev, h),
    )
    conn.commit()


def audit_trail(conn: sqlite3.Connection, case_id: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM audit_log WHERE case_id = ? ORDER BY seq", (case_id,)).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail_json"])} for r in rows]


def verify_audit_chain(conn: sqlite3.Connection) -> tuple[bool, int | None]:
    """Returns (ok, first_bad_seq)."""
    prev = GENESIS
    for r in conn.execute("SELECT * FROM audit_log ORDER BY seq"):
        expected = _entry_hash(prev, r["ts"], r["case_id"], r["actor"], r["action"], r["detail_json"])
        if r["prev_hash"] != prev or r["hash"] != expected:
            return False, r["seq"]
        prev = r["hash"]
    return True, None


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

def save_case(conn, case_id: str, intake_json: str, status: str,
              extraction_json: str | None = None, result_json: str | None = None,
              due_date: str | None = None) -> None:
    now = _now()
    conn.execute(
        """INSERT INTO cases (case_id, intake_json, extraction_json, result_json, status, due_date, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(case_id) DO UPDATE SET
             extraction_json=COALESCE(excluded.extraction_json, cases.extraction_json),
             result_json=COALESCE(excluded.result_json, cases.result_json),
             status=excluded.status,
             due_date=COALESCE(excluded.due_date, cases.due_date),
             updated_at=excluded.updated_at""",
        (case_id, intake_json, extraction_json, result_json, status, due_date, now, now),
    )
    conn.commit()


def get_case(conn, case_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if not r:
        return None
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


def list_cases(conn, status: str | None = None) -> list[dict]:
    # Earliest regulatory deadline first; cases without a clock after.
    q = "SELECT case_id, status, due_date, created_at FROM cases"
    args: tuple = ()
    if status:
        q += " WHERE status = ?"
        args = (status,)
    q += " ORDER BY due_date IS NULL, due_date, created_at"
    return [dict(r) for r in conn.execute(q, args)]


# ---------------------------------------------------------------------------
# Downstream records
# ---------------------------------------------------------------------------

def insert_record(conn, record_id: str, system: str, destination: str, case_id: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO external_records (record_id, system, destination, case_id, payload_json, created_at) VALUES (?,?,?,?,?,?)",
        (record_id, system, destination, case_id, json.dumps(payload, default=str), _now()),
    )
    conn.commit()


def update_record_payload(conn, record_id: str, payload: dict) -> None:
    conn.execute("UPDATE external_records SET payload_json = ? WHERE record_id = ?",
                 (json.dumps(payload, default=str), record_id))
    conn.commit()


def records_for_case(conn, case_id: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM external_records WHERE case_id = ? ORDER BY created_at", (case_id,)).fetchall()
    return [{**{k: r[k] for k in r.keys() if k != "payload_json"}, "payload": json.loads(r["payload_json"])} for r in rows]


def records_for_destination(conn, destination: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM external_records WHERE destination = ? ORDER BY created_at", (destination,)).fetchall()
    return [{**{k: r[k] for k in r.keys() if k != "payload_json"}, "payload": json.loads(r["payload_json"])} for r in rows]
