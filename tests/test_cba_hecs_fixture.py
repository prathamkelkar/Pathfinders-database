"""
Reference fixture test — CONTEXT.md section 2.

CBA's undocumented HECS-HELP serviceability policy:
- years_remaining < 1  -> excluded entirely from serviceability calculations
- 1 <= years_remaining <= 5 -> reduced buffer (3% default down to 1%)

This test seeds `production_rules` directly (bypassing extraction/review, which don't
exist yet) and asserts that `db.query` can answer both branches of the policy correctly.

Expected to fail right now: db/query.py is a stub with no query logic yet. That failure
is the point — this test defines the contract the query interface must satisfy.
"""

from datetime import date

import pytest
from sqlalchemy.orm import Session

from db.models import Base, ProductionRule, Source


@pytest.fixture()
def session(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    sess: Session = sessionmaker(bind=engine)()
    yield sess
    sess.close()


@pytest.fixture()
def cba_hecs_rules(session):
    source = Source(
        entity_name="CBA",
        parent_entity=None,
        source_tier="broker_sourced",
        url="https://example.com/broker-notes/cba-hecs",
        retrieved_date=date(2026, 1, 1),
        raw_content="Broker-sourced note describing CBA's undocumented HECS serviceability treatment.",
    )
    session.add(source)
    session.flush()

    exclusion_rule = ProductionRule(
        lender="CBA",
        debt_type="HECS_HELP",
        conditions={"years_remaining_max": 1},
        effect={"treatment": "excluded_from_serviceability"},
        source_tier="broker_sourced",
        source_id=source.id,
        effective_from=date(2025, 1, 1),
        effective_to=None,
        confidence="corroborated_broker_source",
        conflicting_sources=False,
    )

    reduced_buffer_rule = ProductionRule(
        lender="CBA",
        debt_type="HECS_HELP",
        conditions={"years_remaining_min": 1, "years_remaining_max": 5},
        effect={
            "treatment": "reduced_buffer",
            "default_buffer_pct": 3.0,
            "buffer_pct": 1.0,
        },
        source_tier="broker_sourced",
        source_id=source.id,
        effective_from=date(2025, 1, 1),
        effective_to=None,
        confidence="corroborated_broker_source",
        conflicting_sources=False,
    )

    session.add_all([exclusion_rule, reduced_buffer_rule])
    session.commit()
    return session


def test_hecs_under_one_year_remaining_is_excluded(cba_hecs_rules):
    from db.query import get_serviceability_treatment

    result = get_serviceability_treatment(
        cba_hecs_rules, lender="CBA", debt_type="HECS_HELP", years_remaining=0.5
    )
    assert result["treatment"] == "excluded_from_serviceability"


def test_hecs_three_years_remaining_gets_reduced_buffer(cba_hecs_rules):
    from db.query import get_serviceability_treatment

    result = get_serviceability_treatment(
        cba_hecs_rules, lender="CBA", debt_type="HECS_HELP", years_remaining=3
    )
    assert result["treatment"] == "reduced_buffer"
    assert result["buffer_pct"] == 1.0
