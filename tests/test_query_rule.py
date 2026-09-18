"""
Tests for db.query.query_rule() -- the general-purpose, deterministic
read-only interface. Covers the three cases specifically asked for: the CBA
HECS case, a no-data lookup, and a conflicting_sources=True case.
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, ProductionRule, Rate, Source
from db.query import query_rule


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


def make_source(session, **overrides):
    defaults = dict(
        entity_name="CBA",
        source_tier="broker_sourced",
        url=None,
        retrieved_date=date(2026, 1, 1),
        raw_content="broker note",
    )
    defaults.update(overrides)
    source = Source(**defaults)
    session.add(source)
    session.flush()
    return source


def make_rule(session, source, **overrides):
    defaults = dict(
        lender="CBA",
        debt_type="HECS_HELP",
        conditions={},
        effect={},
        source_tier=source.source_tier,
        effective_from=date(2025, 1, 1),
        effective_to=None,
        confidence="corroborated_broker_source",
        conflicting_sources=False,
        conflict_note=None,
        source_id=source.id,
    )
    defaults.update(overrides)
    rule = ProductionRule(**defaults)
    session.add(rule)
    session.flush()
    return rule


# --- The CBA HECS case (CONTEXT.md section 2) ---


@pytest.fixture()
def cba_hecs_rules(session):
    source = make_source(
        session,
        entity_name="CBA",
        source_tier="broker_sourced",
        url=None,
        retrieved_date=date(2026, 1, 1),
    )
    exclusion = make_rule(
        session,
        source,
        conditions={"years_remaining_max": 1},
        effect={"treatment": "excluded_from_serviceability"},
    )
    buffer_rule = make_rule(
        session,
        source,
        conditions={"years_remaining_min": 1, "years_remaining_max": 5},
        effect={"treatment": "reduced_buffer", "default_buffer_pct": 3.0, "buffer_pct": 1.0},
    )
    return {"source": source, "exclusion": exclusion, "buffer_rule": buffer_rule}


def test_cba_hecs_under_one_year_excluded(session, cba_hecs_rules):
    result = query_rule(session, lender="CBA", debt_type="HECS_HELP", years_remaining=0.5)

    assert result.found is True
    assert result.conflicting_sources is False
    assert result.match.effect["treatment"] == "excluded_from_serviceability"
    assert result.match.confidence == "corroborated_broker_source"
    assert result.match.source.entity_name == "CBA"
    assert result.match.source.source_tier == "broker_sourced"


def test_cba_hecs_three_years_reduced_buffer(session, cba_hecs_rules):
    result = query_rule(session, lender="CBA", debt_type="HECS_HELP", years_remaining=3)

    assert result.found is True
    assert result.match.effect["treatment"] == "reduced_buffer"
    assert result.match.effect["buffer_pct"] == 1.0
    assert result.match.source.source_id == cba_hecs_rules["source"].id


def test_cba_hecs_citation_traces_back_to_real_source(session, cba_hecs_rules):
    result = query_rule(session, lender="CBA", debt_type="HECS_HELP", years_remaining=0.5)
    assert result.match.source.retrieved_date == date(2026, 1, 1)
    assert result.match.source.url is None  # broker-sourced, no public document -- expected


# --- No data ---


def test_no_data_returns_clean_not_found_not_error(session):
    result = query_rule(session, lender="Totally Unknown Lender", debt_type="unicorn_loan", years_remaining=3)

    assert result.found is False
    assert result.match is None
    assert result.conflicting_sources is False
    assert result.conflicts == []
    assert result.rates == []
    assert "No production rule found" in result.message
    assert "Totally Unknown Lender" in result.message


def test_no_data_when_conditions_dont_match_any_rule(session, cba_hecs_rules):
    # years_remaining=100 satisfies neither the exclusion (<1) nor the buffer (1-5) rule
    result = query_rule(session, lender="CBA", debt_type="HECS_HELP", years_remaining=100)
    assert result.found is False
    assert result.match is None


# --- Conflicting sources ---


def test_conflicting_sources_surfaces_both_rather_than_picking_one(session):
    source_a = make_source(session, entity_name="NAB", retrieved_date=date(2025, 6, 1))
    source_b = make_source(session, entity_name="NAB", retrieved_date=date(2026, 3, 1))

    rule_a = make_rule(
        session,
        source_a,
        lender="NAB",
        debt_type="HECS_HELP",
        conditions={"years_remaining_max": 5},
        effect={"treatment": "excluded_from_serviceability"},
        conflicting_sources=False,
    )
    make_rule(
        session,
        source_b,
        lender="NAB",
        debt_type="HECS_HELP",
        conditions={"years_remaining_max": 5},
        effect={"treatment": "reduced_buffer", "buffer_pct": 2.0},
        conflicting_sources=True,
        conflict_note=f"Conflicts with production_rule id={rule_a.id} at source_tier=broker_sourced",
    )

    result = query_rule(session, lender="NAB", debt_type="HECS_HELP", years_remaining=3)

    assert result.found is True
    assert result.conflicting_sources is True
    assert len(result.conflicts) == 2
    effects_seen = {tuple(sorted(c.effect.items())) for c in result.conflicts}
    assert len(effects_seen) == 2  # both distinct effects present, neither silently dropped
    assert "conflicting production rules found" in result.message


def test_conflicting_sources_flag_alone_triggers_surface_even_if_filter_only_matches_one(session):
    """
    Even if the caller's params happen to only "match" one of the two rules directly
    via _conditions_satisfied, a conflicting_sources=True flag on the winning rule must
    still pull in its documented counterpart rather than being silently dropped.
    """
    source_a = make_source(session, entity_name="ANZ", retrieved_date=date(2025, 1, 1))
    source_b = make_source(session, entity_name="ANZ", retrieved_date=date(2026, 1, 1))

    rule_a = make_rule(
        session,
        source_a,
        lender="ANZ",
        debt_type="personal_loan",
        conditions={},
        effect={"early_repayment_fee": False},
        conflicting_sources=False,
    )
    make_rule(
        session,
        source_b,
        lender="ANZ",
        debt_type="personal_loan",
        conditions={},
        effect={"early_repayment_fee": True},
        conflicting_sources=True,
        conflict_note=f"Conflicts with production_rule id={rule_a.id}",
    )

    result = query_rule(session, lender="ANZ", debt_type="personal_loan")

    assert result.conflicting_sources is True
    assert len(result.conflicts) == 2


# --- Rates ---


def test_rates_included_when_relevant(session, cba_hecs_rules):
    source = cba_hecs_rules["source"]
    rate = Rate(
        lender="CBA",
        product_name="Standard Variable Home Loan",
        rate_type="variable",
        rate_pct=6.19,
        effective_from=date(2026, 1, 1),
        effective_to=None,
        source_id=source.id,
    )
    session.add(rate)
    session.commit()

    result = query_rule(session, lender="CBA", debt_type="HECS_HELP", years_remaining=0.5)

    assert len(result.rates) == 1
    assert result.rates[0].rate_pct == 6.19
    assert result.rates[0].product_name == "Standard Variable Home Loan"


def test_never_touches_candidate_rules(session, cba_hecs_rules):
    from db.models import CandidateRule

    # a candidate that would answer differently, to prove it's never consulted
    session.add(
        CandidateRule(
            lender="CBA",
            debt_type="HECS_HELP",
            conditions={"years_remaining_max": 1},
            effect={"treatment": "SHOULD_NEVER_BE_RETURNED"},
            source_tier="broker_sourced",
            effective_from=date(2025, 1, 1),
            confidence="single_anecdotal_source",
            conflicting_sources=False,
            source_id=cba_hecs_rules["source"].id,
        )
    )
    session.commit()

    result = query_rule(session, lender="CBA", debt_type="HECS_HELP", years_remaining=0.5)
    assert result.match.effect["treatment"] == "excluded_from_serviceability"
