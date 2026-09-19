"""
Tests for PolicyAreaCoverage -- the not_applicable status that is distinct from
null/missing.
"""
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, PolicyAreaCoverage, Source
from ingestion.policy_coverage import get_coverage, record_coverage


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


@pytest.fixture()
def source(session):
    src = Source(entity_name="Wisr", source_tier="lender_official", url="https://example.com/fees",
                 retrieved_date=date(2026, 9, 18), raw_content="Early Repayment Fee: Nil")
    session.add(src); session.flush()
    return src


def test_never_assessed_is_none_not_not_applicable(session):
    """The core distinction: no row means unassessed, which is NOT a finding."""
    assert get_coverage(session, lender="Afterpay", policy_area="discharge") is None


def test_not_applicable_is_a_recorded_positive_finding(session):
    record_coverage(session, lender="Afterpay", policy_area="discharge", status="not_applicable",
                    rationale="BNPL -- no secured loan, nothing to discharge.")
    cov = get_coverage(session, lender="Afterpay", policy_area="discharge")
    assert cov is not None
    assert cov.status == "not_applicable"
    assert "BNPL" in cov.rationale


def test_rationale_is_mandatory(session):
    with pytest.raises(ValueError, match="rationale is required"):
        record_coverage(session, lender="Afterpay", policy_area="discharge",
                        status="not_applicable", rationale="")


def test_invalid_status_and_policy_area_rejected(session):
    with pytest.raises(ValueError, match="status must be one of"):
        record_coverage(session, lender="X", policy_area="discharge", status="dunno", rationale="r")
    with pytest.raises(ValueError, match="policy_area must be one of"):
        record_coverage(session, lender="X", policy_area="invented", status="not_applicable", rationale="r")


def test_applicable_found_cites_its_evidence(session, source):
    cov = record_coverage(session, lender="Wisr", policy_area="break_cost", status="applicable_found",
                          rationale="Own product page states Early Repayment Fee: Nil.", source=source)
    assert cov.source_id == source.id
    assert cov.source.url == "https://example.com/fees"


def test_reassessment_overwrites_rather_than_duplicating(session, source):
    record_coverage(session, lender="Wisr", policy_area="break_cost", status="applicable_not_found",
                    rationale="Not found yet.")
    record_coverage(session, lender="Wisr", policy_area="break_cost", status="applicable_found",
                    rationale="Found: Early Repayment Fee Nil.", source=source)
    rows = session.query(PolicyAreaCoverage).filter_by(lender="Wisr", policy_area="break_cost").all()
    assert len(rows) == 1
    assert rows[0].status == "applicable_found"


def test_three_states_are_all_distinguishable(session):
    record_coverage(session, lender="A", policy_area="break_cost", status="not_applicable", rationale="r")
    record_coverage(session, lender="B", policy_area="break_cost", status="applicable_not_found", rationale="r")
    record_coverage(session, lender="C", policy_area="break_cost", status="applicable_found", rationale="r")
    got = {l: get_coverage(session, lender=l, policy_area="break_cost").status for l in "ABC"}
    assert got == {"A": "not_applicable", "B": "applicable_not_found", "C": "applicable_found"}
    assert get_coverage(session, lender="D", policy_area="break_cost") is None  # unassessed
