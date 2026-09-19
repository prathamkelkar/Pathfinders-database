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


# --- transient API failures ---------------------------------------------------
# A read timeout is exactly what retrying is for. The llm_call was originally
# outside the try block, so one timeout propagated out of extract_candidates and
# killed an entire multi-document run: the Pepper Money source-15 timeout took the
# Afterpay and APRA sources with it, untried.


def test_a_transient_api_timeout_is_retried_not_fatal(session, cba_source, monkeypatch):
    from openai import APITimeoutError

    import ingestion.extraction.extract_rules as mod
    monkeypatch.setattr(mod, "RETRY_DELAY_SECONDS", 0)

    calls = []

    def flaky(entity_name, text):
        calls.append(1)
        if len(calls) == 1:
            raise APITimeoutError(request=None)
        return '[{"debt_type": "home_loan", "conditions": {}, "effect": {"ok": true}}]'

    created = extract_candidates(session, cba_source, llm_call=flaky)

    assert len(calls) == 2
    assert len(created) == 1


def test_persistent_api_failure_raises_extraction_failed_not_the_raw_error(session, cba_source, monkeypatch):
    """Callers distinguish 'nothing extractable' from 'the model failed' on this
    exception type; leaking a transport error breaks that contract."""
    from openai import APITimeoutError

    import ingestion.extraction.extract_rules as mod
    monkeypatch.setattr(mod, "RETRY_DELAY_SECONDS", 0)

    def always_times_out(entity_name, text):
        raise APITimeoutError(request=None)

    with pytest.raises(ExtractionFailedError):
        extract_candidates(session, cba_source, llm_call=always_times_out)


def test_request_timeout_is_sized_above_the_token_budget():
    """Guards the pairing that broke: max_tokens was raised without the timeout."""
    import ingestion.extraction.extract_rules as mod

    assert mod.REQUEST_TIMEOUT_SECONDS >= 600, (
        "REQUEST_TIMEOUT_SECONDS must be sized against max_tokens -- a large "
        "max_tokens with a short timeout times out mid-generation every attempt."
    )


# --- chunking large documents -------------------------------------------------
# NVIDIA's gateway returns 504 after ~300s regardless of our client timeout. A
# 69k-char product guide asking for "every explicit rule" cannot finish inside
# that budget, and raising max_tokens made it worse. The fix is less work per call.


def test_a_small_document_is_not_chunked(session, cba_source):
    from ingestion.extraction.extract_rules import _chunk_text
    assert len(_chunk_text("short document")) == 1


def test_a_large_document_is_split_into_overlapping_chunks():
    from ingestion.extraction.extract_rules import MAX_CHUNK_CHARS, _chunk_text

    text = ("Clause about early repayment. " * 3000)
    chunks = _chunk_text(text)

    assert len(chunks) > 1
    assert all(len(c) <= MAX_CHUNK_CHARS + 100 for c in chunks)
    # reassembly must cover the whole document -- no silently dropped tail
    assert chunks[-1].rstrip().endswith("early repayment.")


def test_every_chunk_is_sent_to_the_model(session, cba_source):
    cba_source.raw_content = "Rule text. " * 6000  # ~66k chars
    calls = []

    def recording_llm(entity, text):
        calls.append(text)
        return '[{"debt_type": "home_loan", "conditions": {}, "effect": {"n": %d}}]' % len(calls)

    created = extract_candidates(session, cba_source, llm_call=recording_llm)

    assert len(calls) > 1
    assert len(created) == len(calls)


def test_overlap_duplicates_are_removed(session, cba_source):
    """Chunks overlap so a rule spanning a boundary survives; the same rule then
    comes back twice and must not be stored twice."""
    cba_source.raw_content = "Rule text. " * 6000

    def same_rule_every_chunk(entity, text):
        return '[{"debt_type": "home_loan", "conditions": {"a": 1}, "effect": {"b": 2}}]'

    created = extract_candidates(session, cba_source, llm_call=same_rule_every_chunk)

    assert len(created) == 1


def test_one_failed_chunk_does_not_lose_the_others(session, cba_source, monkeypatch):
    import ingestion.extraction.extract_rules as mod
    monkeypatch.setattr(mod, "RETRY_DELAY_SECONDS", 0)
    cba_source.raw_content = "Rule text. " * 6000
    calls = []

    def flaky(entity, text):
        calls.append(text)
        if len(calls) <= mod.MAX_EXTRACTION_ATTEMPTS:   # first chunk fails every attempt
            raise ValueError("unparseable")
        return '[{"debt_type": "home_loan", "conditions": {}, "effect": {"ok": true}}]'

    created = extract_candidates(session, cba_source, llm_call=flaky)

    assert len(created) >= 1  # later chunks survived the first chunk's failure


def test_all_chunks_failing_still_raises(session, cba_source, monkeypatch):
    import ingestion.extraction.extract_rules as mod
    monkeypatch.setattr(mod, "RETRY_DELAY_SECONDS", 0)
    cba_source.raw_content = "Rule text. " * 6000

    with pytest.raises(ExtractionFailedError):
        extract_candidates(session, cba_source, llm_call=lambda e, t: "no json here")
