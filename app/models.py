"""Domain models for case triage and routing.

Three layers, kept deliberately separate:
- ``IntakeCase``     — what Use Case 1 (intake) hands us. Raw, untrusted text.
- ``CaseExtraction`` — what Claude pulls out of that text. Every fact carries
  the verbatim quote it was taken from so it can be grounded deterministically.
- ``TriageResult``   — what the deterministic rules engine and router decide.
  No LLM output is used here without first passing grounding.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Intake (input from Use Case 1)
# ---------------------------------------------------------------------------

class Channel(str, Enum):
    phone = "phone"
    email = "email"
    web_form = "web_form"
    chat = "chat"
    fax = "fax"


class Context(str, Enum):
    """Regulatory context drives which reporting timelines apply."""
    postmarketing = "postmarketing"
    clinical_trial = "clinical_trial"


class IntakeCase(BaseModel):
    case_id: str
    channel: Channel
    received_at: datetime = Field(
        description="When the company first became aware of the information "
        "(any employee or agent). This is regulatory Day 0, not triage time."
    )
    country: str = "US"
    context: Context = Context.postmarketing
    caller_role: str | None = Field(
        default=None, description="As captured at intake, e.g. patient, HCP, caregiver."
    )
    caller_contact_available: bool = False
    transcript: str


# ---------------------------------------------------------------------------
# Extraction (Claude output — every fact is quote-backed)
# ---------------------------------------------------------------------------

class Intent(str, Enum):
    adverse_event = "adverse_event"
    product_complaint = "product_complaint"
    medical_information = "medical_information"
    other = "other"


class IntentFinding(BaseModel):
    intent: Intent
    evidence: str = Field(description="Verbatim quote from the transcript supporting this intent.")


class SeriousnessCriterion(str, Enum):
    death = "death"
    life_threatening = "life_threatening"
    hospitalization = "hospitalization"
    disability = "disability"
    congenital_anomaly = "congenital_anomaly"
    medically_important = "medically_important"


class SeriousnessFinding(BaseModel):
    criterion: SeriousnessCriterion
    evidence: str = Field(description="Verbatim quote supporting this criterion.")


class AdverseEventFinding(BaseModel):
    term: str = Field(description="Short reported event term, e.g. 'injection site hematoma'.")
    evidence: str = Field(description="Verbatim quote describing the event.")
    seriousness: list[SeriousnessFinding] = Field(
        default_factory=list,
        description="Only criteria explicitly supported by the transcript.",
    )
    seriousness_uncertain: bool = Field(
        default=False,
        description="True if the transcript hints at seriousness but does not "
        "clearly establish it (e.g. 'went to the ER' without admission).",
    )


class ProductMention(BaseModel):
    name: str = Field(description="Product name as stated (brand or generic).")
    evidence: str
    lot_number: str | None = None
    lot_evidence: str | None = None


class PersonInfo(BaseModel):
    identifiable: bool = Field(
        description="True if at least one identifier is present: initials, age, "
        "sex, date of birth, or other identifying detail. A bare 'a patient' is "
        "not identifiable."
    )
    identifiers: list[str] = Field(default_factory=list, description="e.g. ['age 54', 'female'].")
    evidence: str | None = None


class CaseExtraction(BaseModel):
    intents: list[IntentFinding]
    patient: PersonInfo
    reporter: PersonInfo
    reporter_is_hcp: bool | None = None
    products: list[ProductMention] = Field(default_factory=list)
    adverse_events: list[AdverseEventFinding] = Field(default_factory=list)
    complaint_description: str | None = Field(
        default=None, description="Product defect/malfunction as described, if any."
    )
    complaint_evidence: str | None = None
    sample_available: bool | None = Field(
        default=None, description="Whether the caller still has the product/device to return."
    )
    medical_question: str | None = None
    medical_question_evidence: str | None = None
    event_onset_date: date | None = None
    ambiguities: list[str] = Field(
        default_factory=list,
        description="Anything unclear that a human triage lead should look at.",
    )


# ---------------------------------------------------------------------------
# Deterministic triage output
# ---------------------------------------------------------------------------

class Destination(str, Enum):
    drug_safety = "drug_safety"          # PV — Argus / Vault Safety
    product_quality = "product_quality"  # QA — TrackWise / Veeva QMS
    medical_information = "medical_information"  # MI — Service Cloud
    human_triage = "human_triage"


class RuleResult(BaseModel):
    rule_id: str
    passed: bool
    detail: str


class ReportingClock(BaseModel):
    day_zero: date
    serious: bool
    expected: bool | None  # None = could not determine; treated as unexpected
    category: str          # e.g. "15-day expedited", "7-day expedited", "periodic"
    due_date: date | None


class Route(BaseModel):
    destination: Destination
    reason: str
    follow_up_required: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    external_system: str | None = None
    external_id: str | None = None


class CaseStatus(str, Enum):
    auto_routed = "auto_routed"
    pending_review = "pending_review"
    review_approved = "review_approved"
    failed = "failed"


class TriageResult(BaseModel):
    case_id: str
    status: CaseStatus
    intents: list[Intent]
    rule_results: list[RuleResult]
    valid_icsr: bool | None  # None when there's no AE
    clock: ReportingClock | None
    routes: list[Route]
    confidence: float
    review_reasons: list[str] = Field(default_factory=list)
    ungrounded_facts: list[str] = Field(default_factory=list)
