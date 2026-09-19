"""
Tests for query_refinancing() -- the unified read path for break costs, discharge
timelines, switching rules and refinance offers.

The behaviour that matters most here isn't finding rules, it's explaining their
ABSENCE. query_rule()'s found=False conflates "this lender has no such concept",
"it exists and we haven't found it", and "nobody ever checked". Conflating the
first two is what sends someone scraping for a document that cannot exist, or
lets a real policy be recorded as non-existent.
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, PolicyAreaCoverage, ProductionRule, RefinanceOffer, Source
from db.query import query_refinancing


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


@pytest.fixture()
def source(session):
    src = Source(
        entity_name="CBA",
        source_tier="lender_official",
        url="https://example.com/tcs.pdf",
        retrieved_date=date(2026, 9, 1),
        raw_content="...",
    )
    session.add(src)
    session.flush()
    return src


def add_rule(session, source, *, policy_area, effect, lender="CBA", debt_type="home_loan",
             source_tier="lender_official", verification_status=None, conditions=None,
             effective_from=date(2026, 1, 1), effective_to=None):
    rule = ProductionRule(
        lender=lender,
        debt_type=debt_type,
        policy_area=policy_area,
        conditions=conditions if conditions is not None else {},
        effect=effect,
        source_tier=source_tier,
        effective_from=effective_from,
        effective_to=effective_to,
        confidence="official_document",
        conflicting_sources=False,
        verification_status=verification_status,
        source_id=source.id,
    )
    session.add(rule)
    session.flush()
    return rule


def test_break_cost_and_discharge_rules_are_returned_separately(session, source):
    add_rule(session, source, policy_area="break_cost", effect={"era_charged": True})
    add_rule(session, source, policy_area="discharge", effect={"timeline_days": 10})

    result = query_refinancing(session, lender="CBA", as_of=date(2026, 9, 19))

    assert result.found is True
    assert [m.effect for m in result.break_cost_rules] == [{"era_charged": True}]
    assert [m.effect for m in result.discharge_rules] == [{"timeline_days": 10}]
    assert result.switching_rules == []


def test_every_returned_rule_carries_a_source_citation(session, source):
    add_rule(session, source, policy_area="break_cost", effect={"era_charged": True})

    result = query_refinancing(session, lender="CBA", as_of=date(2026, 9, 19))

    citation = result.break_cost_rules[0].source
    assert citation.source_id == source.id
    assert citation.url == "https://example.com/tcs.pdf"
    assert citation.source_tier == "lender_official"
    assert citation.retrieved_date == date(2026, 9, 1)


def test_unverified_layer3_rule_is_flagged_and_rolled_up(session, source):
    """9c: an uncorroborated lead must never read like a settled fact -- and a
    caller reading only the top level of the result must still see the warning."""
    broker_src = Source(entity_name="CBA", source_tier="broker_sourced", url=None,
                        retrieved_date=date(2026, 9, 1), raw_content="broker tip")
    session.add(broker_src)
    session.flush()
    add_rule(session, broker_src, policy_area="discharge", effect={"timeline_days": 36},
             source_tier="broker_sourced", verification_status="needs_corroboration")

    result = query_refinancing(session, lender="CBA", as_of=date(2026, 9, 19))

    assert result.discharge_rules[0].confidence_warning is not None
    assert "UNVERIFIED INSIGHT" in result.discharge_rules[0].confidence_warning
    assert len(result.confidence_warnings) == 1


def test_corroborated_rule_carries_no_warning(session, source):
    broker_src = Source(entity_name="CBA", source_tier="broker_sourced", url=None,
                        retrieved_date=date(2026, 9, 1), raw_content="two brokerages")
    session.add(broker_src)
    session.flush()
    add_rule(session, broker_src, policy_area="discharge", effect={"timeline_days": 12},
             source_tier="broker_sourced", verification_status="corroborated")

    result = query_refinancing(session, lender="CBA", as_of=date(2026, 9, 19))

    assert result.discharge_rules[0].confidence_warning is None
    assert result.confidence_warnings == []


# --- the absence cases: the whole reason this function exists -----------------


def test_not_applicable_is_distinguished_from_missing(session, source):
    """Afterpay has no loan to discharge. That is an answer, not a gap."""
    session.add(PolicyAreaCoverage(
        lender="Afterpay", policy_area="discharge", status="not_applicable",
        rationale="Unsecured BNPL -- no security taken, so no title to release.",
        source_id=source.id, assessed_on=date(2026, 9, 19)))
    session.commit()

    result = query_refinancing(session, lender="Afterpay", as_of=date(2026, 9, 19))

    assert result.found is False
    assert result.coverage["discharge"].status == "not_applicable"
    assert "no security taken" in result.coverage["discharge"].rationale
    assert "discharge=not_applicable" in result.message


def test_applicable_not_found_is_distinguished_from_not_applicable(session, source):
    """Wisr writes secured car loans, so a release process must exist -- we just
    haven't found it. This must NOT read the same as Afterpay."""
    session.add(PolicyAreaCoverage(
        lender="Wisr", policy_area="discharge", status="applicable_not_found",
        rationale="Secured vehicle loans, so a security release process exists; not documented.",
        source_id=source.id, assessed_on=date(2026, 9, 19)))
    session.commit()

    result = query_refinancing(session, lender="Wisr", as_of=date(2026, 9, 19))

    assert result.coverage["discharge"].status == "applicable_not_found"
    assert result.coverage["discharge"].status != "not_applicable"


def test_never_assessed_is_null_and_says_so(session):
    result = query_refinancing(session, lender="NeverChecked", as_of=date(2026, 9, 19))

    assert result.found is False
    assert result.coverage["break_cost"].status is None
    assert result.coverage["discharge"].status is None
    assert "never checked" in result.message.lower()


# --- offers -------------------------------------------------------------------


def test_active_offer_is_returned_with_citation(session, source):
    session.add(RefinanceOffer(
        lender="CBA", offer_name="Spring cashback", cashback_amount_aud=3000.0,
        eligibility_conditions={}, clawback_conditions={}, other_benefits={},
        offer_expiry_date=date(2026, 12, 31), effective_from=date(2026, 9, 1),
        source_id=source.id))
    session.commit()

    result = query_refinancing(session, lender="CBA", as_of=date(2026, 9, 19))

    assert result.found is True
    assert result.offers[0].cashback_amount_aud == 3000.0
    assert result.offers[0].source.source_id == source.id


def test_expired_offer_is_excluded_by_default_and_warned_when_included(session, source):
    session.add(RefinanceOffer(
        lender="CBA", offer_name="Lapsed", cashback_amount_aud=2000.0,
        eligibility_conditions={}, clawback_conditions={}, other_benefits={},
        offer_expiry_date=date(2026, 6, 30), effective_from=date(2026, 1, 1),
        source_id=source.id))
    session.commit()

    assert query_refinancing(session, lender="CBA", as_of=date(2026, 9, 19)).offers == []

    included = query_refinancing(session, lender="CBA", as_of=date(2026, 9, 19),
                                 include_expired_offers=True)
    assert included.offers[0].is_expired is True
    assert "EXPIRED OFFER" in included.offers[0].staleness_warning
    assert len(included.confidence_warnings) == 1


# --- conflicts and filters ----------------------------------------------------


def test_disagreement_within_one_policy_area_is_flagged(session, source):
    add_rule(session, source, policy_area="discharge", effect={"timeline_days": 10})
    add_rule(session, source, policy_area="discharge", effect={"timeline_days": 28})

    result = query_refinancing(session, lender="CBA", as_of=date(2026, 9, 19))

    assert result.conflicting_sources is True


def test_different_policy_areas_disagreeing_is_not_a_conflict(session, source):
    add_rule(session, source, policy_area="break_cost", effect={"fee": 100})
    add_rule(session, source, policy_area="discharge", effect={"fee": 350})

    result = query_refinancing(session, lender="CBA", as_of=date(2026, 9, 19))

    assert result.conflicting_sources is False


def test_list_valued_effects_do_not_crash_the_conflict_check(session, source):
    """Refinancing effects routinely hold lists (timeline ranges); an earlier
    implementation built a set of raw dict items and raised TypeError on these."""
    add_rule(session, source, policy_area="discharge",
             effect={"timeline_business_days_range": [10, 14]})
    add_rule(session, source, policy_area="discharge",
             effect={"timeline_business_days_range": [10, 14]})

    result = query_refinancing(session, lender="CBA", as_of=date(2026, 9, 19))

    assert result.conflicting_sources is False
    assert len(result.discharge_rules) == 2


def test_a_lower_tier_rule_does_not_conflict_with_a_higher_tier_one(session, source):
    """4c: the authority hierarchy resolves this, it is not an escalation."""
    broker_src = Source(entity_name="CBA", source_tier="broker_sourced", url=None,
                        retrieved_date=date(2026, 9, 1), raw_content="tip")
    session.add(broker_src)
    session.flush()
    add_rule(session, source, policy_area="discharge", effect={"timeline_days": 10})
    add_rule(session, broker_src, policy_area="discharge", effect={"timeline_days": 40},
             source_tier="broker_sourced", verification_status="needs_corroboration")

    result = query_refinancing(session, lender="CBA", as_of=date(2026, 9, 19))

    assert result.conflicting_sources is False


def test_debt_type_filter_narrows_results(session, source):
    add_rule(session, source, policy_area="break_cost", effect={"a": 1}, debt_type="home_loan")
    add_rule(session, source, policy_area="break_cost", effect={"b": 2}, debt_type="car_loan")

    result = query_refinancing(session, lender="CBA", debt_type="car_loan", as_of=date(2026, 9, 19))

    assert [m.effect for m in result.break_cost_rules] == [{"b": 2}]


def test_as_of_excludes_a_rule_superseded_before_that_date(session, source):
    add_rule(session, source, policy_area="break_cost", effect={"old": True},
             effective_from=date(2025, 1, 1), effective_to=date(2026, 1, 1))

    assert query_refinancing(session, lender="CBA", as_of=date(2026, 9, 19)).break_cost_rules == []
    assert query_refinancing(session, lender="CBA", as_of=date(2025, 6, 1)).break_cost_rules != []


def test_candidate_rules_are_never_surfaced(session, source):
    """Section 6: unreviewed candidates must never be returned as authoritative."""
    from db.models import CandidateRule

    session.add(CandidateRule(
        lender="CBA", debt_type="home_loan", policy_area="break_cost",
        conditions={}, effect={"leaked": True}, source_tier="lender_official",
        effective_from=date(2026, 1, 1), confidence="official_document",
        conflicting_sources=False, source_id=source.id))
    session.commit()

    result = query_refinancing(session, lender="CBA", as_of=date(2026, 9, 19))

    assert result.found is False
    assert result.break_cost_rules == []
