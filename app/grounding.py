"""Deterministic grounding: every fact Claude extracts must quote the transcript.

A fact whose evidence quote can't be found in the source text is treated as
unsupported — it's removed before the rules engine sees it and recorded so the
case goes to a human. This is the main guard against hallucinated facts
driving a regulatory decision.
"""

from __future__ import annotations

import re

from .models import CaseExtraction

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "—": "-", "–": "-"})


def _normalize(text: str) -> str:
    text = text.translate(_QUOTES).lower()
    text = re.sub(r"[^\w\s'\-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_grounded(quote: str | None, transcript: str) -> bool:
    """True if the quote (or every '...'-separated piece of it) appears in the transcript."""
    if not quote or not quote.strip():
        return False
    source = _normalize(transcript)
    pieces = [_normalize(p) for p in re.split(r"\.\.\.|…", quote)]
    pieces = [p for p in pieces if p]
    return bool(pieces) and all(p in source for p in pieces)


def ground_extraction(extraction: CaseExtraction, transcript: str) -> tuple[CaseExtraction, list[str]]:
    """Return a copy with unsupported facts removed, plus a list describing what was removed."""
    x = extraction.model_copy(deep=True)
    dropped: list[str] = []

    kept_intents = []
    for f in x.intents:
        if is_grounded(f.evidence, transcript):
            kept_intents.append(f)
        else:
            dropped.append(f"intent '{f.intent.value}' (quote not found: {f.evidence!r})")
    x.intents = kept_intents

    kept_products = []
    for p in x.products:
        if not is_grounded(p.evidence, transcript):
            dropped.append(f"product '{p.name}' (quote not found)")
            continue
        if p.lot_number and not is_grounded(p.lot_evidence or p.lot_number, transcript):
            dropped.append(f"lot number '{p.lot_number}' (quote not found)")
            p.lot_number, p.lot_evidence = None, None
        kept_products.append(p)
    x.products = kept_products

    kept_events = []
    for e in x.adverse_events:
        if not is_grounded(e.evidence, transcript):
            dropped.append(f"adverse event '{e.term}' (quote not found)")
            continue
        kept_ser = []
        for s in e.seriousness:
            if is_grounded(s.evidence, transcript):
                kept_ser.append(s)
            else:
                dropped.append(f"seriousness '{s.criterion.value}' for '{e.term}' (quote not found)")
                e.seriousness_uncertain = True
        e.seriousness = kept_ser
        kept_events.append(e)
    x.adverse_events = kept_events

    for person, label in ((x.patient, "patient"), (x.reporter, "reporter")):
        if person.identifiable and person.evidence and not is_grounded(person.evidence, transcript):
            dropped.append(f"{label} identifiers (quote not found)")
            person.identifiable, person.identifiers = False, []

    if x.complaint_description and not is_grounded(x.complaint_evidence, transcript):
        dropped.append("complaint description (quote not found)")
        x.complaint_description, x.complaint_evidence = None, None

    if x.medical_question and not is_grounded(x.medical_question_evidence, transcript):
        dropped.append("medical question (quote not found)")
        x.medical_question, x.medical_question_evidence = None, None

    return x, dropped
