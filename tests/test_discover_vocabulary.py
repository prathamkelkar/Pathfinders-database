"""
Tests for the vocabulary-discovery pass.

The LLM is injected throughout. What matters here isn't the model's wording but
the guard around it: a phrase the model did not actually copy from the document
must never reach TOPIC_KEYWORDS, because a hallucinated keyword is permanent
dead weight that silently never matches.
"""

import json
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, Source
from ingestion.extraction.discover_vocabulary import (
    discover_for_source,
    find_suspicious_absences,
    novel_phrases,
    verify_phrases,
)


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


def make_source(session, raw_content, lender="Zip Co"):
    source = Source(
        entity_name=lender,
        source_tier="lender_official",
        url="https://example.com/terms",
        retrieved_date=date(2026, 9, 19),
        raw_content=raw_content,
    )
    session.add(source)
    session.flush()
    return source


# The real Afterpay wording that the keyword list missed.
AFTERPAY_LIKE = "You may make early payments. If you repay the full amount outstanding, no further instalments are due."


def test_verified_phrase_must_occur_verbatim_in_the_document():
    items = [
        {"topic": "break_cost", "phrase": "you may make early payments", "quote": "..."},
        {"topic": "break_cost", "phrase": "you can pay early anytime", "quote": "..."},  # paraphrase
    ]
    verified, unverified = verify_phrases(AFTERPAY_LIKE, items, "break_cost")

    assert [v["phrase"] for v in verified] == ["you may make early payments"]
    assert [u["phrase"] for u in unverified] == ["you can pay early anytime"]


def test_phrases_for_a_different_topic_are_ignored():
    items = [{"topic": "discharge", "phrase": "you may make early payments", "quote": "..."}]
    verified, unverified = verify_phrases(AFTERPAY_LIKE, items, "break_cost")
    assert verified == [] and unverified == []


def test_only_large_documents_with_no_hits_are_flagged(session):
    big_absent = make_source(session, "lorem ipsum " * 3000)  # large, no vocabulary at all
    # Large and matches BOTH topics -- a document matching only one would still be
    # (correctly) flagged for the other, which is not what this test is about.
    make_source(session, "early repayment fee applies. discharge authority required. " * 2000)
    make_source(session, "nothing relevant here")  # small: below the size threshold

    suspicious = find_suspicious_absences(session)

    assert {s.id for s, _ in suspicious} == {big_absent.id}
    assert {topic for _, topic in suspicious} == {"break_cost", "discharge"}


def test_discovery_reports_verified_and_unverified_separately(session):
    source = make_source(session, AFTERPAY_LIKE)

    def fake_llm(text):
        return json.dumps(
            [
                {"topic": "break_cost", "phrase": "you may make early payments", "quote": AFTERPAY_LIKE},
                {"topic": "break_cost", "phrase": "invented wording", "quote": "not in the document"},
            ]
        )

    result = discover_for_source(source, "break_cost", llm_call=fake_llm)

    assert [v["phrase"] for v in result.verified] == ["you may make early payments"]
    assert [u["phrase"] for u in result.unverified] == ["invented wording"]
    assert result.error is None


def test_a_bad_model_response_is_recorded_not_raised(session):
    """One unusable response must not abort an audit over many documents."""
    source = make_source(session, AFTERPAY_LIKE)

    result = discover_for_source(source, "break_cost", llm_call=lambda text: "sorry, I cannot help")

    assert result.error is not None
    assert result.verified == []


def test_novel_phrases_excludes_vocabulary_we_already_have(session):
    source = make_source(
        session,
        "An early repayment fee of $300 applies. You may settle the account in advance.",
    )

    def fake_llm(text):
        return json.dumps(
            [
                # already covered by TOPIC_KEYWORDS -- must not be reported as novel
                {"topic": "break_cost", "phrase": "early repayment fee", "quote": "..."},
                # wording the vocabulary has no entry for
                {"topic": "break_cost", "phrase": "settle the account in advance", "quote": "..."},
            ]
        )

    result = discover_for_source(source, "break_cost", llm_call=fake_llm)
    novel = novel_phrases([result])

    assert novel["break_cost"] == {"settle the account in advance"}


# --- truncated responses ------------------------------------------------------
# A reply cut off mid-string is not a failed audit: the findings before the cut
# are complete and get verified against the document anyway. The real Westpac
# source-5 audit was thrown away this way, taking a genuine phrasing with it.


def test_complete_objects_are_salvaged_from_a_truncated_response(session):
    source = make_source(session, "You may have to pay an early termination charge on exit.")

    truncated = (
        '[\n  {"topic": "break_cost", "phrase": "early termination charge", "quote": "pay an early termination charge"},\n'
        '  {"topic": "break_cost", "phrase": "on exit", "qu'  # cut off mid-key
    )

    result = discover_for_source(source, "break_cost", llm_call=lambda text: truncated)

    assert result.error is None
    assert [v["phrase"] for v in result.verified] == ["early termination charge"]


def test_a_brace_inside_a_quoted_value_does_not_split_an_object(session):
    source = make_source(session, "Clause {5.6} covers an early termination charge.")

    raw = '[{"topic": "break_cost", "phrase": "early termination charge", "quote": "Clause {5.6} covers"}]'

    result = discover_for_source(source, "break_cost", llm_call=lambda text: raw)

    assert [v["phrase"] for v in result.verified] == ["early termination charge"]


def test_a_response_with_no_parseable_object_is_still_an_error(session):
    source = make_source(session, "irrelevant text")

    result = discover_for_source(source, "break_cost", llm_call=lambda text: "[ {broken")

    assert result.error is not None
    assert result.verified == []
