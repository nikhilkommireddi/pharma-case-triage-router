import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.extraction import ExtractionError  # noqa: E402
from app.models import CaseExtraction, IntakeCase  # noqa: E402

PEN_TRANSCRIPT = (
    "Hi, I'm calling about my mother, she's 72. She uses the Cardiolex autoinjector "
    "and yesterday the pen jammed halfway through the injection. She ended up with "
    "a big bruise, a hematoma on her thigh where she injected. Lot number is CX4471. "
    "I still have the pen if you need it back. You can reach me at this number."
)


def make_case(transcript=PEN_TRANSCRIPT, **kw) -> IntakeCase:
    base = dict(
        case_id="C-001", channel="phone",
        received_at=datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc),
        caller_role="caregiver", caller_contact_available=True, transcript=transcript,
    )
    base.update(kw)
    return IntakeCase.model_validate(base)


def pen_extraction(**overrides) -> CaseExtraction:
    data = {
        "intents": [
            {"intent": "adverse_event", "evidence": "a hematoma on her thigh where she injected"},
            {"intent": "product_complaint", "evidence": "the pen jammed halfway through the injection"},
        ],
        "patient": {"identifiable": True, "identifiers": ["female", "age 72"],
                    "evidence": "my mother, she's 72"},
        "reporter": {"identifiable": True, "identifiers": ["caregiver (daughter/son)"],
                     "evidence": "I'm calling about my mother"},
        "reporter_is_hcp": False,
        "products": [{"name": "Cardiolex autoinjector", "evidence": "the Cardiolex autoinjector",
                      "lot_number": "CX4471", "lot_evidence": "Lot number is CX4471"}],
        "adverse_events": [{"term": "injection site hematoma",
                            "evidence": "a big bruise, a hematoma on her thigh"}],
        "complaint_description": "Autoinjector pen jammed mid-injection",
        "complaint_evidence": "the pen jammed halfway through the injection",
        "sample_available": True,
        "event_onset_date": "2026-09-30",
    }
    data.update(overrides)
    return CaseExtraction.model_validate(data)


class FakeExtractor:
    def __init__(self, extraction: CaseExtraction | None = None, error: str | None = None):
        self.extraction, self.error = extraction, error

    def extract(self, case):
        if self.error:
            raise ExtractionError(self.error)
        return self.extraction


@pytest.fixture
def conn():
    # Set TEST_DATABASE_URL to run the suite against Postgres.
    import os
    url = os.getenv("TEST_DATABASE_URL", ":memory:")
    engine = db.connect(url)
    yield engine
    if url != ":memory:":
        with engine.begin() as c:
            c.exec_driver_sql("DROP TABLE IF EXISTS audit_log, external_records, cases CASCADE")
    engine.dispose()
