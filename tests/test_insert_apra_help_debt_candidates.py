from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from db.models import Base, CandidateRule, Source
from ingestion.insert_apra_help_debt_candidates import insert_apra_help_debt_candidates
from ingestion.scrapers.apra_apg223 import DOCUMENTS


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


@pytest.fixture()
def apra_sources(session):
    sources = []
    for doc in DOCUMENTS:
        source = Source(
            entity_name="APRA",
            source_tier="regulator_guidance",
            url=doc.url,
            retrieved_date=date(2026, 9, 18),
            raw_content="stub",
        )
        session.add(source)
        sources.append(source)
    session.flush()
    return sources


def test_raises_if_sources_not_scraped_yet(session):
    with pytest.raises(LookupError, match="run `python3 -m ingestion.scrapers.apra_apg223`"):
        insert_apra_help_debt_candidates(session)


def test_inserts_two_candidates_with_correct_distinction(session, apra_sources):
    candidates = insert_apra_help_debt_candidates(session)

    assert len(candidates) == 2
    serviceability, dti = candidates

    assert serviceability.lender == "APRA"
    assert serviceability.debt_type == "HECS_HELP"
    assert serviceability.effect["mandatory"] is False
    assert serviceability.effect["treatment"] == "may_exclude_from_serviceability"
    assert serviceability.conditions == {"expected_repayment_within_months": 12}

    assert dti.effect["mandatory"] is True
    assert dti.effect["treatment"] == "excluded_from_dti_calculation"
    assert dti.conditions == {}

    # the crucial nuance the task asked for: MAY vs MUST must not be conflated
    assert serviceability.effect["mandatory"] != dti.effect["mandatory"]


def test_commencement_date_and_status_set_correctly(session, apra_sources):
    candidates = insert_apra_help_debt_candidates(session)
    for c in candidates:
        assert c.commencement_date == date(2025, 9, 30)
        assert c.legislation_status == "in_force"
        assert c.source_tier == "regulator_guidance"
        assert c.confidence == "official_document"


def test_not_llm_extracted(session, apra_sources):
    """extraction_prompt_version must be None -- these were hand-verified, not LLM-produced."""
    candidates = insert_apra_help_debt_candidates(session)
    assert all(c.extraction_prompt_version is None for c in candidates)


def test_linked_to_correct_respective_sources(session, apra_sources):
    candidates = insert_apra_help_debt_candidates(session)
    serviceability, dti = candidates

    assert serviceability.source.url == DOCUMENTS[0].url  # APG 223
    assert dti.source.url == DOCUMENTS[1].url  # ARS 223.0


def test_description_flags_lender_specific_thresholds_still_needed(session, apra_sources):
    candidates = insert_apra_help_debt_candidates(session)
    serviceability, _ = candidates
    assert "Layer 3 sourcing per lender" in serviceability.description


def test_never_writes_to_production_rules(session, apra_sources):
    from db.models import ProductionRule

    insert_apra_help_debt_candidates(session)
    assert session.scalars(select(ProductionRule)).all() == []
    assert session.scalars(select(CandidateRule)).all().__len__() == 2
