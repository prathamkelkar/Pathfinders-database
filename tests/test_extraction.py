"""
Tests for ingestion/extraction/extract_rules.py.

These inject a canned `llm_call` rather than hitting the real NVIDIA API --
deterministic, no network, no API cost, and lets us assert on exact
hallucination-avoidance behaviour without depending on model variance.
"""

import json
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from db.models import Base, CandidateRule, Source
from ingestion.extraction.extract_rules import (
    EXTRACTION_PROMPT_VERSION,
    MAX_EXTRACTION_ATTEMPTS,
    ExtractionFailedError,
    extract_candidates,
)


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


@pytest.fixture()
def cba_source(session):
    source = Source(
        entity_name="CBA",
        source_tier="lender_official",
        url="https://example.com/cba-pds.pdf",
        retrieved_date=date(2026, 1, 1),
        raw_content=(
            "Early repayment: a $0 redraw fee applies for redraws of $500 or more "
            "made via internet banking, effective from 1 July 2024. Redraws made in "
            "branch incur a $50 fee. This document does not address how HECS-HELP "
            "debt is treated in our servicing assessment."
        ),
    )
    session.add(source)
    session.flush()
    return source


def test_extraction_does_not_hallucinate_absent_fields(session, cba_source):
    """
    The source text states a redraw fee rule but never says which debt_type it
    applies to, and never states an effective_to date. A canned response mimics
    a correctly-behaving LLM: null for both rather than a guessed value.
    """

    def fake_llm_call(entity_name, source_text):
        return json.dumps(
            [
                {
                    "debt_type": None,  # not stated in the text -- must not be guessed
                    "conditions": {"redraw_amount_min": 500, "channel": "internet_banking"},
                    "effect": {"redraw_fee": 0},
                    "effective_from": "2024-07-01",
                    "effective_to": None,  # not stated -- must not be guessed
                    "legislation_status": None,
                    "enacted_date": None,
                    "commencement_date": None,
                }
            ]
        )

    candidates = extract_candidates(session, cba_source, llm_call=fake_llm_call)

    # debt_type is a required column and was correctly left null by the LLM --
    # the extraction script must skip it rather than invent a debt_type, so no
    # candidate should be inserted for this item.
    assert candidates == []
    assert session.scalars(select(CandidateRule)).all() == []


def test_extraction_inserts_only_fields_explicitly_present(session, cba_source):
    """A rule with debt_type present should be inserted, with unspecified fields left null/empty."""

    def fake_llm_call(entity_name, source_text):
        return json.dumps(
            [
                {
                    "debt_type": "home_loan",
                    "conditions": {"redraw_amount_min": 500, "channel": "internet_banking"},
                    "effect": {"redraw_fee": 0},
                    "effective_from": "2024-07-01",
                    "effective_to": None,
                    "legislation_status": None,
                    "enacted_date": None,
                    "commencement_date": None,
                }
            ]
        )

    candidates = extract_candidates(session, cba_source, llm_call=fake_llm_call)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.lender == "CBA"
    assert candidate.debt_type == "home_loan"
    assert candidate.effective_from == date(2024, 7, 1)
    assert candidate.effective_to is None
    assert candidate.legislation_status is None
    assert candidate.enacted_date is None
    assert candidate.commencement_date is None
    assert candidate.extraction_prompt_version == EXTRACTION_PROMPT_VERSION
    assert candidate.confidence == "official_document"
    assert candidate.source_id == cba_source.id

    # never written to production_rules
    from db.models import ProductionRule

    assert session.scalars(select(ProductionRule)).all() == []


def test_extraction_never_invents_the_undocumented_cba_hecs_rule(session, cba_source):
    """
    Sanity check for the specific failure mode CONTEXT.md section 2 warns about:
    the CBA HECS serviceability treatment is broker-sourced, not documented in any
    public PDS. Even though the source text explicitly mentions HECS-HELP (to
    say it's *not* addressed), a correctly-behaving extraction must not fabricate
    a HECS_HELP rule out of thin air. This test uses a canned response representing
    correct behaviour -- it would fail if extract_candidates itself injected extra
    rules beyond what the LLM returned.
    """

    def fake_llm_call(entity_name, source_text):
        assert "HECS" in source_text  # confirms the text does mention it
        return json.dumps([])  # correct behaviour: nothing extractable about HECS here

    candidates = extract_candidates(session, cba_source, llm_call=fake_llm_call)

    assert candidates == []
    hecs_candidates = session.scalars(
        select(CandidateRule).where(CandidateRule.debt_type == "HECS_HELP")
    ).all()
    assert hecs_candidates == []


# --- Retry logic ---


def test_recovers_on_a_later_attempt_after_malformed_responses(session, cba_source, monkeypatch):
    """
    About a third of real documents were observed hitting one malformed/unparseable
    response before an identical retry succeeded. This simulates exactly that:
    the first two calls return garbage, the third returns valid JSON.
    """
    monkeypatch.setattr("ingestion.extraction.extract_rules.time.sleep", lambda _: None)

    calls = []

    def flaky_llm_call(entity_name, source_text):
        calls.append(1)
        if len(calls) < 3:
            return "this is not json at all"
        return json.dumps([{"debt_type": "home_loan", "conditions": {}, "effect": {"recovered": True}}])

    candidates = extract_candidates(session, cba_source, llm_call=flaky_llm_call)

    assert len(calls) == 3
    assert len(candidates) == 1
    assert candidates[0].effect == {"recovered": True}


def test_succeeds_immediately_without_retrying_when_first_response_is_valid(session, cba_source, monkeypatch):
    monkeypatch.setattr("ingestion.extraction.extract_rules.time.sleep", lambda _: None)

    calls = []

    def fake_llm_call(entity_name, source_text):
        calls.append(1)
        return json.dumps([{"debt_type": "home_loan", "conditions": {}, "effect": {}}])

    extract_candidates(session, cba_source, llm_call=fake_llm_call)

    assert len(calls) == 1  # no retries needed


def test_raises_extraction_failed_error_after_exhausting_all_attempts(session, cba_source, monkeypatch):
    monkeypatch.setattr("ingestion.extraction.extract_rules.time.sleep", lambda _: None)

    calls = []

    def always_malformed(entity_name, source_text):
        calls.append(1)
        return "still not json"

    with pytest.raises(ExtractionFailedError):
        extract_candidates(session, cba_source, llm_call=always_malformed)

    assert len(calls) == MAX_EXTRACTION_ATTEMPTS


def test_failed_extraction_inserts_no_candidates(session, cba_source, monkeypatch):
    """A failure-after-retries must not leave partial/garbage candidate rows behind."""
    monkeypatch.setattr("ingestion.extraction.extract_rules.time.sleep", lambda _: None)

    def always_malformed(entity_name, source_text):
        return "not json"

    with pytest.raises(ExtractionFailedError):
        extract_candidates(session, cba_source, llm_call=always_malformed)

    assert session.scalars(select(CandidateRule)).all() == []


def test_extraction_failed_error_is_distinct_from_empty_result(session, cba_source, monkeypatch):
    """
    The whole point of ExtractionFailedError: callers must be able to tell "genuinely
    nothing extractable" (a clean [] -- returns normally) apart from "the model never
    responded usably" (raises) rather than treating both as "0 candidates".
    """
    monkeypatch.setattr("ingestion.extraction.extract_rules.time.sleep", lambda _: None)

    clean_empty = extract_candidates(session, cba_source, llm_call=lambda e, t: json.dumps([]))
    assert clean_empty == []  # not an exception

    with pytest.raises(ExtractionFailedError):
        extract_candidates(session, cba_source, llm_call=lambda e, t: "garbage")


# --- truncated responses ------------------------------------------------------
# A reply cut off by max_tokens is not a failed extraction -- every object before
# the cut is complete. Discarding the whole reply loses them, and because
# temperature is 0 the retries truncate identically, so all three attempts burn on
# the same failure. Observed on Pepper Money source 15 (69k chars in, >44k out).


def test_complete_objects_are_salvaged_from_a_truncated_response(session, cba_source):
    truncated = (
        '[\n'
        '  {"debt_type": "home_loan", "conditions": {"income_type": "overtime"}, '
        '"effect": {"shading_pct": 80}},\n'
        '  {"debt_type": "home_loan", "conditions": {"income_type": "bonus"}, "eff'  # cut mid-key
    )

    created = extract_candidates(session, cba_source, llm_call=lambda e, t: truncated)

    assert len(created) == 1
    assert created[0].conditions == {"income_type": "overtime"}


def test_a_brace_inside_a_quoted_value_does_not_split_an_object(session, cba_source):
    raw = (
        '[{"debt_type": "home_loan", "conditions": {}, '
        '"effect": {"note": "see clause {5.6} of the schedule"}}]'
    )

    created = extract_candidates(session, cba_source, llm_call=lambda e, t: raw)

    assert len(created) == 1
    assert created[0].effect["note"] == "see clause {5.6} of the schedule"


def test_a_reply_with_no_complete_object_still_fails(session, cba_source):
    """Salvage must not turn an unusable response into a silent empty success --
    'nothing extractable' and 'the model failed' have to stay distinguishable."""
    with pytest.raises(ExtractionFailedError):
        extract_candidates(session, cba_source, llm_call=lambda e, t: '[ {"debt_type": "home')
