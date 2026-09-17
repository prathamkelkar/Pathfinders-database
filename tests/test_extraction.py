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
from ingestion.extraction.extract_rules import EXTRACTION_PROMPT_VERSION, extract_candidates


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
