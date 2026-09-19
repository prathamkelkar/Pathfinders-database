"""
Tests for the targeted break-cost/discharge re-extraction pass (prompt version 2).

The LLM is always injected here -- these exercise the keyword scan, excerpt
windowing, coverage classification and version tagging, which is all the logic
that actually matters for answering "found vs genuinely absent".
"""

import json
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from db.models import Base, CandidateRule, Source
from ingestion.extraction.extract_loan_mechanics import (
    LOAN_MECHANICS_PROMPT_VERSION,
    build_excerpts,
    extract_loan_mechanics_from_source,
    find_keyword_hits,
)


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


def make_source(session, raw_content, lender="CBA"):
    source = Source(
        entity_name=lender,
        source_tier="lender_official",
        url="https://example.com/tcs.pdf",
        retrieved_date=date(2026, 9, 1),
        raw_content=raw_content,
    )
    session.add(source)
    session.flush()
    return source


BREAK_COST_TEXT = (
    "Section 14. If you repay your Fixed Rate loan early, an early repayment adjustment "
    "may be payable. The adjustment is calculated as the difference between the wholesale "
    "swap rate at the time of fixing and at the time of prepayment, applied over the "
    "balance of the fixed period."
)

DISCHARGE_TEXT = (
    "Section 22. When your loan is repaid in full, you must submit a discharge authority. "
    "We will prepare the release of mortgage and lodge it with the titles office, which "
    "typically takes 10 business days."
)

IRRELEVANT_TEXT = (
    "Section 3. Interest is calculated daily on the outstanding balance and debited monthly. "
    "You may make additional repayments to your variable rate loan at any time."
)


def fake_llm_returning(items):
    def _call(entity_name, source_text):
        return json.dumps(items)

    return _call


def fake_llm_empty(entity_name, source_text):
    return json.dumps([])


# --- Keyword scan + excerpt windowing ---


def test_find_keyword_hits_is_case_insensitive_and_ordered():
    text = "Early Repayment Adjustment applies. Later, a break cost may apply."
    hits = find_keyword_hits(text, ("early repayment adjustment", "break cost"))
    assert [kw for _, kw in hits] == ["early repayment adjustment", "break cost"]


def test_build_excerpts_merges_overlapping_windows():
    text = "x" * 100 + "break cost" + "y" * 50 + "break fee" + "z" * 100
    hits = find_keyword_hits(text, ("break cost", "break fee"))
    excerpt = build_excerpts(text, hits, radius=80)
    # two hits 50 chars apart with an 80-char radius overlap -> one contiguous chunk
    assert "[...]" not in excerpt


def test_build_excerpts_is_capped():
    text = ("break cost " + "filler " * 200) * 20
    hits = find_keyword_hits(text, ("break cost",))
    excerpt = build_excerpts(text, hits, radius=1200, max_chars=5000)
    assert len(excerpt) <= 5000


def test_build_excerpts_sends_far_less_than_the_full_document():
    text = "filler " * 20000 + BREAK_COST_TEXT + "filler " * 20000
    hits = find_keyword_hits(text, ("early repayment adjustment",))
    excerpt = build_excerpts(text, hits)
    assert "early repayment adjustment" in excerpt.lower()
    assert len(excerpt) < len(text) / 10


# --- Coverage classification: the found / absent / present-not-extracted distinction ---


def test_topic_with_no_vocabulary_is_genuinely_absent_and_costs_no_llm_call(session):
    source = make_source(session, IRRELEVANT_TEXT)

    def exploding_llm(entity_name, source_text):
        raise AssertionError("no LLM call should be made for a topic with no vocabulary")

    result = extract_loan_mechanics_from_source(session, source, llm_call=exploding_llm)

    assert result.topics["discharge"].coverage == "absent"
    assert result.topics["discharge"].keyword_hits == 0


def test_topic_with_vocabulary_and_extractable_rules_is_found(session):
    source = make_source(session, BREAK_COST_TEXT)
    llm = fake_llm_returning([
        {
            "policy_area": "break_cost",
            "debt_type": "home_loan",
            "conditions": {"loan_type": "Fixed Rate"},
            "effect": {"calculation_basis": "wholesale swap rate differential"},
        }
    ])

    result = extract_loan_mechanics_from_source(session, source, llm_call=llm)

    assert result.topics["break_cost"].coverage == "found"
    assert result.topics["break_cost"].candidates_created == 1
    assert result.topics["break_cost"].keyword_hits > 0


def test_vocabulary_present_but_nothing_extracted_is_not_reported_as_absent(session):
    """The distinction the whole pass exists to make -- this must not be read as
    'go scrape a different document', it means a human should look."""
    source = make_source(session, BREAK_COST_TEXT)

    result = extract_loan_mechanics_from_source(session, source, llm_call=fake_llm_empty)

    topic = result.topics["break_cost"]
    assert topic.coverage == "present_not_extracted"
    assert topic.coverage != "absent"
    assert topic.keyword_hits > 0
    assert topic.candidates_created == 0


def test_both_topics_assessed_independently(session):
    source = make_source(session, BREAK_COST_TEXT + "\n\n" + IRRELEVANT_TEXT)
    llm = fake_llm_returning([
        {"policy_area": "break_cost", "debt_type": "home_loan", "conditions": {}, "effect": {"x": 1}}
    ])

    result = extract_loan_mechanics_from_source(session, source, llm_call=llm)

    assert result.topics["break_cost"].coverage == "found"
    assert result.topics["discharge"].coverage == "absent"


def test_extraction_failure_is_distinct_from_both_found_and_absent(session):
    from ingestion.extraction.extract_rules import ExtractionFailedError

    source = make_source(session, BREAK_COST_TEXT)

    def always_malformed(entity_name, source_text):
        return "not json at all"

    import ingestion.extraction.extract_rules as er

    original_sleep = er.time.sleep
    er.time.sleep = lambda _: None
    try:
        result = extract_loan_mechanics_from_source(session, source, llm_call=always_malformed)
    finally:
        er.time.sleep = original_sleep

    assert result.topics["break_cost"].coverage == "extraction_failed"
    assert result.topics["break_cost"].error is not None


# --- Version tagging + policy_area (4i) ---


def test_candidates_are_tagged_with_the_new_prompt_version(session):
    source = make_source(session, BREAK_COST_TEXT)
    llm = fake_llm_returning([
        {"policy_area": "break_cost", "debt_type": "home_loan", "conditions": {}, "effect": {"a": 1}}
    ])

    extract_loan_mechanics_from_source(session, source, llm_call=llm)

    candidate = session.scalars(select(CandidateRule)).one()
    assert candidate.extraction_prompt_version == LOAN_MECHANICS_PROMPT_VERSION == "2"
    assert candidate.policy_area == "break_cost"


def test_item_policy_area_wins_over_the_topic_default(session):
    source = make_source(session, DISCHARGE_TEXT)
    # scanning the discharge topic, but the model classifies the rule as break_cost
    llm = fake_llm_returning([
        {"policy_area": "break_cost", "debt_type": "home_loan", "conditions": {}, "effect": {"a": 1}}
    ])

    extract_loan_mechanics_from_source(session, source, llm_call=llm)

    candidate = session.scalars(select(CandidateRule)).one()
    assert candidate.policy_area == "break_cost"


def test_unrecognised_policy_area_falls_back_to_the_topic_default(session):
    source = make_source(session, DISCHARGE_TEXT)
    llm = fake_llm_returning([
        {"policy_area": "something_invented", "debt_type": "home_loan", "conditions": {}, "effect": {"a": 1}}
    ])

    extract_loan_mechanics_from_source(session, source, llm_call=llm)

    candidate = session.scalars(select(CandidateRule)).one()
    assert candidate.policy_area == "discharge"  # the scanned topic, not the invented value


def test_candidates_still_link_to_the_full_source_despite_excerpt_override(session):
    """Provenance (4g) must be unaffected by only sending excerpts to the model."""
    source = make_source(session, "filler " * 5000 + BREAK_COST_TEXT)
    llm = fake_llm_returning([
        {"policy_area": "break_cost", "debt_type": "home_loan", "conditions": {}, "effect": {"a": 1}}
    ])

    extract_loan_mechanics_from_source(session, source, llm_call=llm)

    candidate = session.scalars(select(CandidateRule)).one()
    assert candidate.source_id == source.id
    assert candidate.source.url == "https://example.com/tcs.pdf"


def test_pass_writes_only_to_candidate_rules(session):
    from db.models import ProductionRule

    source = make_source(session, BREAK_COST_TEXT)
    llm = fake_llm_returning([
        {"policy_area": "break_cost", "debt_type": "home_loan", "conditions": {}, "effect": {"a": 1}}
    ])

    extract_loan_mechanics_from_source(session, source, llm_call=llm)

    assert session.scalars(select(ProductionRule)).all() == []


def test_extracted_candidates_are_committed_not_just_flushed(session, tmp_path):
    """
    Regression: the v2 runner originally never committed. extract_candidates()
    only flushes, so a long pass that was killed partway through lost every
    candidate it had already paid an LLM call for.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    source = make_source(session, BREAK_COST_TEXT)
    llm = fake_llm_returning([
        {"policy_area": "break_cost", "debt_type": "home_loan", "conditions": {}, "effect": {"a": 1}}
    ])
    extract_loan_mechanics_from_source(session, source, llm_call=llm)

    # A completely separate connection must be able to see the row.
    url = str(session.get_bind().url)
    other = sessionmaker(bind=create_engine(url))()
    try:
        assert other.query(CandidateRule).count() == 1
    finally:
        other.close()


# --- resume behaviour ---------------------------------------------------------
# This pass makes slow paid LLM calls over ~27 documents and has already been
# interrupted once mid-run. Restarting it must neither re-ask the model questions
# it has answered nor write a second copy of the answers.


def _break_cost_llm(entity_name, source_text):
    return json.dumps(
        [
            {
                "policy_area": "break_cost",
                "debt_type": "home_loan",
                "conditions": {"loan_type": "Fixed Rate"},
                "effect": {"calculation_basis": "swap rate differential"},
                "effective_from": None,
                "effective_to": None,
            }
        ]
    )


def test_resume_skips_topics_that_already_have_v2_candidates(session):
    source = make_source(session, BREAK_COST_TEXT)
    extract_loan_mechanics_from_source(session, source, llm_call=_break_cost_llm)

    def fail_if_called(entity_name, source_text):
        raise AssertionError("resume must not re-issue an LLM call for an already-extracted topic")

    result = extract_loan_mechanics_from_source(session, source, llm_call=fail_if_called)

    assert result.topics["break_cost"].coverage == "already_extracted"


def test_rerunning_does_not_duplicate_candidates(session):
    source = make_source(session, BREAK_COST_TEXT)
    extract_loan_mechanics_from_source(session, source, llm_call=_break_cost_llm)
    before = session.scalars(select(CandidateRule)).all()

    extract_loan_mechanics_from_source(session, source, llm_call=_break_cost_llm)

    after = session.scalars(select(CandidateRule)).all()
    assert len(after) == len(before) == 1


def test_no_resume_still_refuses_to_write_a_duplicate(session):
    """resume=False re-asks the model, but an identical answer must not land twice."""
    source = make_source(session, BREAK_COST_TEXT)
    extract_loan_mechanics_from_source(session, source, llm_call=_break_cost_llm)

    result = extract_loan_mechanics_from_source(
        session, source, llm_call=_break_cost_llm, resume=False
    )

    assert result.topics["break_cost"].coverage != "already_extracted"  # it really did re-run
    assert len(session.scalars(select(CandidateRule)).all()) == 1


def test_resume_does_not_skip_a_topic_the_previous_run_never_reached(session):
    """
    The Firstmac 51 case: break_cost completed, then the process was killed before
    discharge. Resuming must still run discharge.
    """
    source = make_source(session, BREAK_COST_TEXT + "\n\n" + DISCHARGE_TEXT)
    extract_loan_mechanics_from_source(
        session, source, llm_call=_break_cost_llm, topics={"break_cost": ("early repayment adjustment",)}
    )

    calls = []

    def recording_llm(entity_name, source_text):
        calls.append(source_text)
        return json.dumps(
            [
                {
                    "policy_area": "discharge",
                    "debt_type": "home_loan",
                    "conditions": {"loan_repaid_in_full": True},
                    "effect": {"typical_timeline_days": 10},
                    "effective_from": None,
                    "effective_to": None,
                }
            ]
        )

    result = extract_loan_mechanics_from_source(session, source, llm_call=recording_llm)

    assert result.topics["break_cost"].coverage == "already_extracted"
    assert result.topics["discharge"].coverage == "found"
    assert len(calls) == 1


def test_a_genuinely_new_rule_from_the_same_source_is_still_stored(session):
    """Dedupe must key on the rule payload, not merely on (source, policy_area)."""
    source = make_source(session, BREAK_COST_TEXT)
    extract_loan_mechanics_from_source(session, source, llm_call=_break_cost_llm)

    def different_rule_llm(entity_name, source_text):
        return json.dumps(
            [
                {
                    "policy_area": "break_cost",
                    "debt_type": "home_loan",
                    "conditions": {"loan_type": "Variable Rate"},  # differs
                    "effect": {"calculation_basis": "swap rate differential"},
                    "effective_from": None,
                    "effective_to": None,
                }
            ]
        )

    extract_loan_mechanics_from_source(
        session, source, llm_call=different_rule_llm, resume=False
    )

    assert len(session.scalars(select(CandidateRule)).all()) == 2


# --- vocabulary regression ----------------------------------------------------
# The keyword list is the single point of failure for the absent/present call: a
# phrasing that isn't in it produces a CONFIDENT false negative, which is the
# worst output this pass can give. Afterpay's Pay Monthly terms scanned as absent
# across 181k chars purely because "early payment" was missing. Every phrasing
# below is one actually observed in a real lender document, so if someone prunes
# the vocabulary these fail rather than silently re-opening the hole.

REAL_WORLD_BREAK_COST_PHRASINGS = [
    # Afterpay Pay Monthly Product Terms cl 5.6 -- the miss that prompted this test
    "You may make early payments.",
    "If you repay the full amount outstanding under a Pay Monthly Order",
    # Wisr personal/car loan pages
    "Early Repayment Fee: Nil",
    "no early exit or repayment fees",
    # MoneyMe AutoPay broker page
    "With no early exit fees, flexible loan terms",
    # CBA / Westpac fixed-rate mortgage language
    "an early repayment adjustment may be payable",
    "break cost may apply if you repay your fixed rate loan early",
    # La Trobe / non-bank language
    "a deferred establishment fee applies",
]

REAL_WORLD_DISCHARGE_PHRASINGS = [
    "you must submit a discharge authority",
    "we will prepare the release of mortgage",
    "request a payout figure",
    "the certificate of title will be released",
]


@pytest.mark.parametrize("phrasing", REAL_WORLD_BREAK_COST_PHRASINGS)
def test_break_cost_vocabulary_catches_real_world_phrasings(phrasing):
    from ingestion.extraction.extract_loan_mechanics import TOPIC_KEYWORDS

    hits = find_keyword_hits(phrasing, TOPIC_KEYWORDS["break_cost"])
    assert hits, f"break_cost vocabulary missed a real lender phrasing: {phrasing!r}"


@pytest.mark.parametrize("phrasing", REAL_WORLD_DISCHARGE_PHRASINGS)
def test_discharge_vocabulary_catches_real_world_phrasings(phrasing):
    from ingestion.extraction.extract_loan_mechanics import TOPIC_KEYWORDS

    hits = find_keyword_hits(phrasing, TOPIC_KEYWORDS["discharge"])
    assert hits, f"discharge vocabulary missed a real lender phrasing: {phrasing!r}"


def test_absent_is_not_reported_as_a_claim_about_the_document():
    """
    The runner must not describe an unmatched scan as 'genuinely absent'. That
    phrasing is what sends someone scraping for a document we already hold, or
    lets a real policy be recorded as non-existent.
    """
    import inspect

    from ingestion.extraction import run_loan_mechanics_pass

    text = inspect.getsource(run_loan_mechanics_pass)
    # Match the verdict STRING, not any mention of the words -- a comment is
    # allowed (and does) discuss the old wording when explaining why it was wrong.
    assert "ABSENT from the sources we hold" not in text
    assert "NO VOCABULARY MATCHED" in text
