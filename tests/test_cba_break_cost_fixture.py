"""
Break-cost/exit-fee fixture test, same pattern as test_cba_hecs_fixture.py --
CONTEXT.md section 3's revised category coverage (break costs, discharge,
switching), and 4e/4g's versioning + source-citation requirements.

Real content: CBA's own Consumer Mortgage Lending Products T&Cs (already
scraped, ingestion/scrapers/cba.py) states a genuine break-cost rule -- a
FORMULA, not a flat number -- for early repayment on a fixed-rate loan beyond
the $10,000/year threshold. This is the real, already-extracted
candidate_rule id=4 (now tagged policy_area="break_cost"); this fixture seeds
the equivalent as a production_rule directly (bypassing the full review flow,
same shortcut test_cba_hecs_fixture.py takes) and asserts query_rule() can
retrieve it correctly, with the formula description intact and a full source
citation -- not just a bare number.
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base, ProductionRule, Source
from db.query import query_rule


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    sess: Session = sessionmaker(bind=engine)()
    yield sess
    sess.close()


@pytest.fixture()
def cba_break_cost_rules(session):
    source = Source(
        entity_name="CBA",
        parent_entity=None,
        source_tier="lender_official",
        url="https://www.commbank.com.au/content/dam/commbank/personal/apply-online/download-printed-forms/utc-home-loan.pdf",
        retrieved_date=date(2026, 9, 17),
        raw_content="Consumer Mortgage Lending Products Terms and Conditions (real, scraped CBA T&Cs).",
    )
    session.add(source)
    session.flush()

    # Within the $10,000/year threshold: no break cost -- real content, candidate_rule id=3.
    within_threshold_rule = ProductionRule(
        lender="CBA",
        debt_type="home_loan",
        policy_area="break_cost",
        conditions={
            "loan_type": "Fixed Rate",
            "action": "partial prepayment",
            "limit_per_fixed_term_year_aud": 10000,
        },
        effect={"administrative_fee_charged": False, "early_repayment_adjustment_charged": False},
        source_tier="lender_official",
        source_id=source.id,
        effective_from=date(2026, 9, 17),
        effective_to=None,
        confidence="official_document",
        conflicting_sources=False,
    )

    # Beyond the threshold: a genuine FORMULA-based break cost -- real content,
    # candidate_rule id=4. This is the case the task specifically asked for: the
    # field holds a description of the calculation method, not a flat dollar figure.
    beyond_threshold_rule = ProductionRule(
        lender="CBA",
        debt_type="home_loan",
        policy_area="break_cost",
        conditions={
            "loan_type": "Fixed Rate",
            "action": "prepayment exceeding $10,000 per fixed term year or full payoff earlier than expected",
        },
        effect={
            "early_repayment_adjustment_charged": True,
            "administrative_fee_charged": True,
            "calculation_basis": (
                "difference between wholesale market swap rate at fixing and at "
                "prepayment for balance of fixed period"
            ),
            "maximum": "estimate of Bank's loss",
        },
        source_tier="lender_official",
        source_id=source.id,
        effective_from=date(2026, 9, 17),
        effective_to=None,
        confidence="official_document",
        conflicting_sources=False,
    )

    session.add_all([within_threshold_rule, beyond_threshold_rule])
    session.commit()
    return {"session": session, "source": source, "beyond_threshold": beyond_threshold_rule}


def test_break_cost_within_threshold_has_no_fee(cba_break_cost_rules):
    session = cba_break_cost_rules["session"]
    result = query_rule(
        session,
        lender="CBA",
        debt_type="home_loan",
        policy_area="break_cost",
        loan_type="Fixed Rate",
        action="partial prepayment",
    )
    assert result.found is True
    assert result.match.effect["early_repayment_adjustment_charged"] is False


def test_break_cost_beyond_threshold_is_formula_based_not_a_flat_number(cba_break_cost_rules):
    session = cba_break_cost_rules["session"]
    result = query_rule(
        session,
        lender="CBA",
        debt_type="home_loan",
        policy_area="break_cost",
        loan_type="Fixed Rate",
        action="prepayment exceeding $10,000 per fixed term year or full payoff earlier than expected",
    )

    assert result.found is True
    assert result.conflicting_sources is False
    effect = result.match.effect
    assert effect["early_repayment_adjustment_charged"] is True

    # The core requirement: this is a description of a calculation METHOD, not a
    # bare dollar amount -- there is no "fee_amount_aud" field here at all, because
    # that would misrepresent a variable, formula-driven cost as a fixed one.
    assert "fee_amount_aud" not in effect
    assert isinstance(effect["calculation_basis"], str)
    assert "wholesale market swap rate" in effect["calculation_basis"]


def test_break_cost_rule_is_versioned_like_any_other_rule(cba_break_cost_rules):
    """CONTEXT.md 4e: same effective-date versioning mechanism as every other rule type."""
    rule = cba_break_cost_rules["beyond_threshold"]
    assert rule.effective_from == date(2026, 9, 17)
    assert rule.effective_to is None


def test_break_cost_rule_has_full_source_citation(cba_break_cost_rules):
    """CONTEXT.md 4g: no rule without a traceable source."""
    session = cba_break_cost_rules["session"]
    result = query_rule(
        session,
        lender="CBA",
        debt_type="home_loan",
        policy_area="break_cost",
        loan_type="Fixed Rate",
        action="prepayment exceeding $10,000 per fixed term year or full payoff earlier than expected",
    )
    citation = result.match.source
    assert citation.entity_name == "CBA"
    assert citation.source_tier == "lender_official"
    assert "utc-home-loan.pdf" in citation.url
    assert citation.retrieved_date == date(2026, 9, 17)


def test_policy_area_disambiguates_from_a_serviceability_rule_with_the_same_conditions(session):
    """
    The exact ambiguity policy_area exists to prevent: a break_cost rule and a
    serviceability rule for the same lender/debt_type that happen to share
    identical `conditions` must NOT be conflated or flagged as conflicting with
    each other -- they're about different things.
    """
    source = Source(
        entity_name="CBA", source_tier="lender_official", url="https://example.com/cba.pdf",
        retrieved_date=date(2026, 1, 1), raw_content="stub",
    )
    session.add(source)
    session.flush()

    shared_conditions = {"loan_type": "Fixed Rate"}
    break_cost_rule = ProductionRule(
        lender="CBA", debt_type="home_loan", policy_area="break_cost",
        conditions=shared_conditions, effect={"calculation_basis": "swap rate differential"},
        source_tier="lender_official", source_id=source.id,
        effective_from=date(2026, 1, 1), confidence="official_document", conflicting_sources=False,
    )
    serviceability_rule = ProductionRule(
        lender="CBA", debt_type="home_loan", policy_area="serviceability",
        conditions=shared_conditions, effect={"assessment_rate_buffer_pct": 3.0},
        source_tier="lender_official", source_id=source.id,
        effective_from=date(2026, 1, 1), confidence="official_document", conflicting_sources=False,
    )
    session.add_all([break_cost_rule, serviceability_rule])
    session.commit()

    result = query_rule(session, lender="CBA", debt_type="home_loan", policy_area="break_cost", loan_type="Fixed Rate")
    assert result.found is True
    assert result.conflicting_sources is False  # NOT flagged despite identical conditions elsewhere
    assert result.match.effect == {"calculation_basis": "swap rate differential"}


# --- Illustrative-only coverage for discharge/switching (synthetic test data, not
# researched claims -- proving the schema generalizes per CONTEXT.md section 2's
# "every schema should be able to represent X" principle, not asserting real facts) ---


def test_discharge_policy_area_shape(session):
    source = Source(
        entity_name="ExampleLender", source_tier="lender_official", url="https://example.com/discharge.pdf",
        retrieved_date=date(2026, 1, 1), raw_content="stub",
    )
    session.add(source)
    session.flush()
    rule = ProductionRule(
        lender="ExampleLender", debt_type="home_loan", policy_area="discharge",
        conditions={"loan_type": "any"},
        effect={
            "process_description": "borrower submits a discharge authority form; lender lodges with land titles office",
            "typical_timeline_days": 10,
            "fee_aud": 350,
        },
        source_tier="lender_official", source_id=source.id,
        effective_from=date(2026, 1, 1), confidence="official_document", conflicting_sources=False,
    )
    session.add(rule)
    session.commit()

    result = query_rule(session, lender="ExampleLender", debt_type="home_loan", policy_area="discharge", loan_type="any")
    assert result.found is True
    assert result.match.effect["typical_timeline_days"] == 10


def test_switching_policy_area_shape(session):
    source = Source(
        entity_name="ExampleLender", source_tier="lender_official", url="https://example.com/switching.pdf",
        retrieved_date=date(2026, 1, 1), raw_content="stub",
    )
    session.add(source)
    session.flush()
    rule = ProductionRule(
        lender="ExampleLender", debt_type="home_loan", policy_area="switching",
        conditions={"from_product": "variable_rate", "to_product": "fixed_rate"},
        effect={
            "requires_full_refinance": False,
            "fee_aud": 0,
            "process_description": "internal rate-type switch via online banking, no new credit assessment required",
        },
        source_tier="lender_official", source_id=source.id,
        effective_from=date(2026, 1, 1), confidence="official_document", conflicting_sources=False,
    )
    session.add(rule)
    session.commit()

    result = query_rule(
        session, lender="ExampleLender", debt_type="home_loan", policy_area="switching",
        from_product="variable_rate", to_product="fixed_rate",
    )
    assert result.found is True
    assert result.match.effect["requires_full_refinance"] is False
