from datetime import date

from app import rules
from app.grounding import ground_extraction, is_grounded
from app.models import CaseStatus, Destination, Intent
from app.routing import decide

from conftest import PEN_TRANSCRIPT, make_case, pen_extraction


# --- grounding --------------------------------------------------------------

def test_verbatim_quote_is_grounded_despite_case_and_punctuation():
    assert is_grounded("The pen JAMMED halfway through the injection.", PEN_TRANSCRIPT)


def test_paraphrase_is_not_grounded():
    assert not is_grounded("the device malfunctioned during use", PEN_TRANSCRIPT)


def test_ellipsis_quote_requires_every_piece():
    assert is_grounded("my mother ... Lot number is CX4471", PEN_TRANSCRIPT)
    assert not is_grounded("my mother ... lot number is ZZ999", PEN_TRANSCRIPT)


def test_hallucinated_seriousness_is_dropped_and_marked_uncertain():
    x = pen_extraction(adverse_events=[{
        "term": "injection site hematoma", "evidence": "a hematoma on her thigh",
        "seriousness": [{"criterion": "hospitalization", "evidence": "she was admitted overnight"}],
    }])
    grounded, dropped = ground_extraction(x, PEN_TRANSCRIPT)
    assert grounded.adverse_events[0].seriousness == []
    assert grounded.adverse_events[0].seriousness_uncertain
    assert any("hospitalization" in d for d in dropped)


# --- minimum criteria & clock -----------------------------------------------

def test_minimum_criteria_all_met():
    assert all(r.passed for r in rules.minimum_criteria(make_case(), pen_extraction()))


def test_unidentifiable_patient_fails_min_criteria_but_still_routes_to_pv():
    x = pen_extraction(patient={"identifiable": False})
    result = decide(make_case(), x, [])
    assert result.valid_icsr is False
    pv = next(r for r in result.routes if r.destination == Destination.drug_safety)
    assert pv.follow_up_required
    assert any("patient identifier" in m for m in pv.missing_fields)
    assert result.clock.due_date is None


def test_listed_nonserious_event_goes_to_periodic():
    clock = rules.reporting_clock(make_case(), pen_extraction(), valid_icsr=True)
    assert clock.expected is True and not clock.serious
    assert clock.category == "periodic report" and clock.due_date is None


def test_serious_unlisted_postmarketing_is_15_day_from_awareness_date():
    x = pen_extraction(adverse_events=[{
        "term": "anaphylaxis", "evidence": "a big bruise",
        "seriousness": [{"criterion": "life_threatening", "evidence": "a big bruise"}],
    }])
    clock = rules.reporting_clock(make_case(), x, valid_icsr=True)
    assert clock.day_zero == date(2026, 10, 1)
    assert clock.category.startswith("15-day")
    assert clock.due_date == date(2026, 10, 16)


def test_serious_listed_postmarketing_is_not_expedited():
    x = pen_extraction(adverse_events=[{
        "term": "bleeding", "evidence": "a big bruise",
        "seriousness": [{"criterion": "hospitalization", "evidence": "a big bruise"}],
    }])
    clock = rules.reporting_clock(make_case(), x, valid_icsr=True)
    assert clock.serious and clock.expected is True
    assert clock.due_date is None


def test_fatal_susar_in_clinical_trial_is_7_day():
    x = pen_extraction(adverse_events=[{
        "term": "cardiac arrest", "evidence": "a big bruise",
        "seriousness": [{"criterion": "death", "evidence": "a big bruise"}],
    }])
    clock = rules.reporting_clock(make_case(context="clinical_trial"), x, valid_icsr=True)
    assert clock.category.startswith("7-day")
    assert clock.due_date == date(2026, 10, 8)


def test_unknown_product_is_treated_as_unexpected():
    x = pen_extraction(
        products=[{"name": "Zentrova", "evidence": "the Cardiolex autoinjector"}],
        adverse_events=[{"term": "headache", "evidence": "a big bruise",
                         "seriousness": [{"criterion": "hospitalization", "evidence": "a big bruise"}]}],
    )
    clock = rules.reporting_clock(make_case(), x, valid_icsr=True)
    assert clock.expected is None
    assert clock.due_date is not None  # conservative: expedited


# --- routing ----------------------------------------------------------------

def test_pen_jam_with_hematoma_forks_to_pv_and_qa_and_auto_routes():
    result = decide(make_case(), pen_extraction(), [])
    dests = {r.destination for r in result.routes}
    assert dests == {Destination.drug_safety, Destination.product_quality}
    assert result.status == CaseStatus.auto_routed
    assert result.confidence >= 0.9


def test_ae_without_ae_intent_is_still_sent_to_pv():
    x = pen_extraction(intents=[{"intent": "product_complaint",
                                 "evidence": "the pen jammed halfway through the injection"}])
    result = decide(make_case(), x, [])
    assert Intent.adverse_event in result.intents
    assert any(r.destination == Destination.drug_safety for r in result.routes)


def test_borderline_seriousness_goes_to_human():
    x = pen_extraction(adverse_events=[{"term": "injection site hematoma", "evidence": "a hematoma on her thigh",
                                        "seriousness_uncertain": True}])
    result = decide(make_case(), x, [])
    assert result.status == CaseStatus.pending_review
    assert result.routes[-1].destination == Destination.human_triage
    assert any("Borderline" in r for r in result.review_reasons)


def test_missing_lot_flags_follow_up_for_qa_and_pv():
    x = pen_extraction(products=[{"name": "Cardiolex autoinjector", "evidence": "the Cardiolex autoinjector"}])
    result = decide(make_case(), x, [])
    qa = next(r for r in result.routes if r.destination == Destination.product_quality)
    pv = next(r for r in result.routes if r.destination == Destination.drug_safety)
    assert "lot / batch number" in qa.missing_fields
    assert any("lot number" in m for m in pv.missing_fields)


def test_other_intent_only_goes_to_human():
    x = pen_extraction(intents=[{"intent": "other", "evidence": "You can reach me at this number"}],
                       adverse_events=[], products=[], complaint_description=None)
    result = decide(make_case(), x, [])
    assert result.status == CaseStatus.pending_review
    assert [r.destination for r in result.routes] == [Destination.human_triage]
