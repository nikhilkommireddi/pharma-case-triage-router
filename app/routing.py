"""Routing matrix + human-in-the-loop gate. Deterministic.

Confidence here is NOT the model's self-reported confidence (that isn't
calibrated). It's a score built from signals we can actually check:
grounding failures, ambiguity the extractor admitted to, uncertain
seriousness, unknown products, and unresolved intent. Any hard trigger forces
review regardless of the score.
"""

from __future__ import annotations

from . import rules
from .models import (
    CaseExtraction,
    CaseStatus,
    Destination,
    IntakeCase,
    Intent,
    Route,
    TriageResult,
)

CONFIDENCE_THRESHOLD = 0.90


def _confidence(x: CaseExtraction, dropped: list[str], expected: bool | None, serious: bool) -> float:
    score = 1.0
    score -= min(0.25 * len(dropped), 0.5)
    score -= min(0.10 * len(x.ambiguities), 0.3)
    if any(e.seriousness_uncertain for e in x.adverse_events):
        score -= 0.15
    if x.adverse_events and expected is None:
        score -= 0.10
    if serious and expected is None:
        score -= 0.10
    if not x.intents or any(f.intent == Intent.other for f in x.intents):
        score -= 0.20
    return round(max(score, 0.0), 2)


def decide(case: IntakeCase, x: CaseExtraction, dropped: list[str]) -> TriageResult:
    intents = sorted({f.intent for f in x.intents}, key=lambda i: i.value)

    # An extracted adverse event always means a safety route, even if the
    # extractor didn't tag the intent — a missed AE is the costliest error.
    if x.adverse_events and Intent.adverse_event not in intents:
        intents.append(Intent.adverse_event)

    mins = rules.minimum_criteria(case, x)
    has_ae = Intent.adverse_event in intents
    valid_icsr = all(r.passed for r in mins) if has_ae else None
    clock = rules.reporting_clock(case, x, bool(valid_icsr)) if has_ae else None
    serious = bool(clock and clock.serious)
    expected = clock.expected if clock else None

    routes: list[Route] = []
    if has_ae:
        missing = rules.missing_for_safety(case, x, mins)
        routes.append(Route(
            destination=Destination.drug_safety,
            reason=("Valid ICSR" if valid_icsr else "Potential AE — minimum criteria incomplete")
            + (f"; {clock.category}" if clock else ""),
            follow_up_required=bool(missing),
            missing_fields=missing,
        ))
    if Intent.product_complaint in intents:
        missing = rules.missing_for_quality(case, x)
        routes.append(Route(
            destination=Destination.product_quality,
            reason="Product quality complaint" + (" linked to an adverse event" if has_ae else ""),
            follow_up_required=bool(missing),
            missing_fields=missing,
        ))
    if Intent.medical_information in intents:
        missing = rules.missing_for_medinfo(case, x)
        routes.append(Route(
            destination=Destination.medical_information,
            reason="Medical information request",
            follow_up_required=bool(missing),
            missing_fields=missing,
        ))

    # Hard triggers: these always need a human, whatever the score says.
    reasons: list[str] = []
    if not routes:
        reasons.append("No routable intent identified")
    if Intent.other in intents:
        reasons.append("Contains an intent outside the routing matrix")
    if any(e.seriousness_uncertain for e in x.adverse_events):
        reasons.append("Borderline seriousness — confirm whether criteria are met")
    if serious and expected is None:
        reasons.append("Serious event for a product not in the reference catalog — expectedness unassessed")
    if dropped:
        reasons.append(f"{len(dropped)} extracted fact(s) could not be found in the transcript")

    confidence = _confidence(x, dropped, expected, serious)
    if confidence < CONFIDENCE_THRESHOLD:
        reasons.append(f"Routing confidence {confidence:.2f} below {CONFIDENCE_THRESHOLD:.2f}")
    reasons.extend(f"Extractor flagged: {a}" for a in x.ambiguities)

    status = CaseStatus.pending_review if reasons else CaseStatus.auto_routed
    if status == CaseStatus.pending_review:
        routes.append(Route(destination=Destination.human_triage, reason="; ".join(reasons[:3])))

    return TriageResult(
        case_id=case.case_id,
        status=status,
        intents=intents,
        rule_results=mins if has_ae else [],
        valid_icsr=valid_icsr,
        clock=clock,
        routes=routes,
        confidence=confidence,
        review_reasons=reasons,
        ungrounded_facts=dropped,
    )
