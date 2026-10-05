"""Deterministic SOP / regulatory rules. No LLM calls in this module.

Rule IDs are stable so they can be cited in the audit trail and in SOPs.

References (simplified for this demo — not legal/regulatory advice):
- Minimum criteria for a valid ICSR: ICH E2D (identifiable patient,
  identifiable reporter, suspect product, adverse event).
- Seriousness criteria: ICH E2A.
- Postmarketing 15-day alert reports for serious AND unexpected events:
  21 CFR 314.80(c)(1).
- Clinical trial SUSARs: 7 calendar days for fatal/life-threatening, 15 days
  otherwise: 21 CFR 312.32(c), ICH E2A.
"""

from __future__ import annotations

import json
from datetime import timedelta
from functools import lru_cache
from pathlib import Path

from .models import (
    CaseExtraction,
    Context,
    IntakeCase,
    ReportingClock,
    RuleResult,
    SeriousnessCriterion,
)

PRODUCTS_PATH = Path(__file__).resolve().parent.parent / "data" / "products.json"


# ---------------------------------------------------------------------------
# Product catalog / reference safety information
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _catalog() -> list[dict]:
    return json.loads(PRODUCTS_PATH.read_text(encoding="utf-8"))["products"]


def lookup_product(name: str) -> dict | None:
    n = name.strip().lower()
    for p in _catalog():
        for alias in p["names"]:
            a = alias.lower()
            if n == a or a in n or n in a:
                return p
    return None


def _term_matches(term: str, labeled: str) -> bool:
    t, l = term.strip().lower(), labeled.lower()
    return t == l or l in t or t in l


def event_expected(term: str, product: dict | None) -> bool | None:
    """True/False against the product's labeled events; None if product unknown."""
    if product is None:
        return None
    return any(_term_matches(term, l) for l in product["labeled_events"])


# ---------------------------------------------------------------------------
# Minimum criteria (valid ICSR)
# ---------------------------------------------------------------------------

def minimum_criteria(case: IntakeCase, x: CaseExtraction) -> list[RuleResult]:
    reporter_ok = x.reporter.identifiable or case.caller_contact_available
    return [
        RuleResult(
            rule_id="PV-MIN-01",
            passed=x.patient.identifiable,
            detail="Identifiable patient: " + (", ".join(x.patient.identifiers) or "none found"),
        ),
        RuleResult(
            rule_id="PV-MIN-02",
            passed=reporter_ok,
            detail="Identifiable reporter: "
            + (", ".join(x.reporter.identifiers) if x.reporter.identifiers else
               ("contact details captured at intake" if case.caller_contact_available else "none found")),
        ),
        RuleResult(
            rule_id="PV-MIN-03",
            passed=bool(x.products),
            detail="Suspect product: " + (", ".join(p.name for p in x.products) or "none found"),
        ),
        RuleResult(
            rule_id="PV-MIN-04",
            passed=bool(x.adverse_events),
            detail="Adverse event: " + (", ".join(e.term for e in x.adverse_events) or "none found"),
        ),
    ]


# ---------------------------------------------------------------------------
# Seriousness, expectedness, reporting clock
# ---------------------------------------------------------------------------

def seriousness(x: CaseExtraction) -> tuple[bool, set[SeriousnessCriterion]]:
    criteria = {s.criterion for e in x.adverse_events for s in e.seriousness}
    return bool(criteria), criteria


def expectedness(x: CaseExtraction) -> bool | None:
    """False if ANY event is unlisted for ANY suspect product it could belong to.

    None means it couldn't be assessed (unknown product) — callers must treat
    that conservatively as unexpected.
    """
    products = [lookup_product(p.name) for p in x.products]
    if not products or any(p is None for p in products):
        return None
    for e in x.adverse_events:
        if not any(event_expected(e.term, p) for p in products):
            return False
    return True


def reporting_clock(case: IntakeCase, x: CaseExtraction, valid_icsr: bool) -> ReportingClock | None:
    if not x.adverse_events:
        return None

    day_zero = case.received_at.date()
    serious, criteria = seriousness(x)
    expected = expectedness(x)
    unexpected = expected is not True  # unknown is treated as unexpected

    if not valid_icsr:
        return ReportingClock(
            day_zero=day_zero, serious=serious, expected=expected,
            category="not yet reportable — minimum criteria incomplete; follow up",
            due_date=None,
        )

    if case.context == Context.clinical_trial:
        if serious and unexpected:
            fatal_or_lt = bool(criteria & {SeriousnessCriterion.death, SeriousnessCriterion.life_threatening})
            days = 7 if fatal_or_lt else 15
            return ReportingClock(
                day_zero=day_zero, serious=True, expected=expected,
                category=f"{days}-day expedited (SUSAR)",
                due_date=day_zero + timedelta(days=days),
            )
        return ReportingClock(
            day_zero=day_zero, serious=serious, expected=expected,
            category="per protocol / aggregate reporting", due_date=None,
        )

    if serious and unexpected:
        return ReportingClock(
            day_zero=day_zero, serious=True, expected=expected,
            category="15-day expedited (alert report)",
            due_date=day_zero + timedelta(days=15),
        )
    return ReportingClock(
        day_zero=day_zero, serious=serious, expected=expected,
        category="periodic report", due_date=None,
    )


# ---------------------------------------------------------------------------
# Completeness checklists per destination
# ---------------------------------------------------------------------------

def missing_for_safety(case: IntakeCase, x: CaseExtraction, mins: list[RuleResult]) -> list[str]:
    labels = {
        "PV-MIN-01": "patient identifier (initials, age, sex or DOB)",
        "PV-MIN-02": "reporter identity / contact details",
        "PV-MIN-03": "suspect product name",
        "PV-MIN-04": "adverse event description",
    }
    missing = [labels[r.rule_id] for r in mins if not r.passed]
    if x.adverse_events and x.event_onset_date is None:
        missing.append("event onset date")
    for p in x.products:
        cat = lookup_product(p.name)
        if cat and cat["is_device_combination"] and not p.lot_number:
            missing.append(f"lot number for {p.name}")
    return missing


def missing_for_quality(case: IntakeCase, x: CaseExtraction) -> list[str]:
    missing = []
    if not x.products:
        missing.append("product name")
    if not any(p.lot_number for p in x.products):
        missing.append("lot / batch number")
    if not x.complaint_description:
        missing.append("description of the defect")
    if x.sample_available is None:
        missing.append("whether the sample is available for return")
    if not case.caller_contact_available:
        missing.append("complainant contact details")
    return missing


def missing_for_medinfo(case: IntakeCase, x: CaseExtraction) -> list[str]:
    missing = []
    if not x.medical_question:
        missing.append("the medical question itself")
    if not case.caller_contact_available:
        missing.append("contact details for the response")
    return missing
