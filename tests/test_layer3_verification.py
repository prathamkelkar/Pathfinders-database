"""
Tests for the Layer 3 corroboration gate (CONTEXT.md 9c):
ingestion/layer3_verification.py, review_candidates.py's promotion default,
and db/query.py's confidence_warning surfacing.
"""

from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from db.models import Base, CandidateRule, CorroboratingSource, ProductionRule, Source
from db.query import query_rule
from ingestion.layer3_verification import add_corroborating_source, compute_verification_status
from ingestion.review_candidates import promote


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


def make_source(session, source_tier="broker_sourced"):
    source = Source(
        entity_name="CBA",
        source_tier=source_tier,
        url=None if source_tier == "broker_sourced" else "https://example.com/doc.pdf",
        retrieved_date=date(2026, 1, 1),
        raw_content="note",
    )
    session.add(source)
    session.flush()
    return source


def make_candidate(session, source, **overrides):
    defaults = dict(
        lender="CBA",
        debt_type="HECS_HELP",
        conditions={"years_remaining_max": 1},
        effect={"treatment": "excluded_from_serviceability"},
        source_tier=source.source_tier,
        effective_from=date(2025, 1, 1),
        confidence="single_anecdotal_source",
        conflicting_sources=False,
        extraction_prompt_version="1",
        source_id=source.id,
    )
    defaults.update(overrides)
    candidate = CandidateRule(**defaults)
    session.add(candidate)
    session.flush()
    return candidate


# --- Promotion defaults (review_candidates.py) ---


def test_broker_sourced_promotion_always_starts_needs_corroboration(session):
    source = make_source(session, "broker_sourced")
    candidate = make_candidate(session, source)

    production_rule = promote(session, candidate, conflict=None)

    assert production_rule.verification_status == "needs_corroboration"


def test_lender_official_promotion_has_no_verification_status(session):
    source = make_source(session, "lender_official")
    candidate = make_candidate(session, source, source_tier="lender_official", confidence="official_document")

    production_rule = promote(session, candidate, conflict=None)

    assert production_rule.verification_status is None


# --- compute_verification_status (pure logic) ---


def _entry(source_type="broker_social", broker_identifier=None, is_direct_confirmation=False):
    return CorroboratingSource(
        source_type=source_type,
        broker_identifier=broker_identifier,
        date=date(2026, 1, 1),
        is_direct_confirmation=is_direct_confirmation,
    )


def test_zero_or_one_source_needs_corroboration():
    assert compute_verification_status([]) == "needs_corroboration"
    assert compute_verification_status([_entry()]) == "needs_corroboration"


def test_two_sources_same_type_same_broker_still_needs_corroboration():
    sources = [_entry(broker_identifier="broker_a"), _entry(broker_identifier="broker_a")]
    assert compute_verification_status(sources) == "needs_corroboration"


def test_two_sources_different_type_is_corroborated():
    sources = [_entry(source_type="broker_social"), _entry(source_type="trade_press")]
    assert compute_verification_status(sources) == "corroborated"


def test_two_sources_same_type_different_broker_is_corroborated():
    sources = [
        _entry(source_type="broker_social", broker_identifier="broker_a"),
        _entry(source_type="broker_social", broker_identifier="broker_b"),
    ]
    assert compute_verification_status(sources) == "corroborated"


def test_direct_confirmation_overrides_everything():
    sources = [_entry(is_direct_confirmation=True)]
    assert compute_verification_status(sources) == "directly_confirmed"


def test_one_known_one_unknown_broker_same_type_is_not_corroborated_regardless_of_order():
    """
    Regression test: a known broker paired with an unknown one must NOT count as
    independent just because one side happens to be named -- otherwise the result
    would depend on which entry is listed first, not on the actual evidence.
    """
    known_first = [
        _entry(source_type="broker_social", broker_identifier="broker_a"),
        _entry(source_type="broker_social", broker_identifier=None),
    ]
    unknown_first = [
        _entry(source_type="broker_social", broker_identifier=None),
        _entry(source_type="broker_social", broker_identifier="broker_a"),
    ]
    assert compute_verification_status(known_first) == "needs_corroboration"
    assert compute_verification_status(unknown_first) == "needs_corroboration"


# --- add_corroborating_source (mutates + persists) ---


def test_add_corroborating_source_upgrades_status(session):
    source = make_source(session, "broker_sourced")
    candidate = make_candidate(session, source)
    rule = promote(session, candidate, conflict=None)
    assert rule.verification_status == "needs_corroboration"

    add_corroborating_source(
        session, rule, source_type="broker_social", evidence_date=date(2026, 2, 1),
        broker_identifier="original_broker", note="the original Reddit post",
    )
    assert rule.verification_status == "needs_corroboration"  # still just one source

    add_corroborating_source(
        session, rule, source_type="trade_press", evidence_date=date(2026, 3, 1),
        url_or_reference="https://theadviser.com.au/example",
    )
    assert rule.verification_status == "corroborated"

    persisted = session.scalars(
        select(CorroboratingSource).where(CorroboratingSource.production_rule_id == rule.id)
    ).all()
    assert len(persisted) == 2


def test_add_corroborating_source_rejects_non_broker_sourced_rule(session):
    source = make_source(session, "lender_official")
    candidate = make_candidate(session, source, source_tier="lender_official", confidence="official_document")
    rule = promote(session, candidate, conflict=None)

    with pytest.raises(ValueError, match="broker_sourced"):
        add_corroborating_source(session, rule, source_type="trade_press", evidence_date=date(2026, 1, 1))


def test_add_corroborating_source_rejects_invalid_source_type(session):
    source = make_source(session, "broker_sourced")
    candidate = make_candidate(session, source)
    rule = promote(session, candidate, conflict=None)

    with pytest.raises(ValueError, match="source_type"):
        add_corroborating_source(session, rule, source_type="a_guess", evidence_date=date(2026, 1, 1))


# --- add_corroborating_source() on a CandidateRule (pre-promotion) ---


def test_add_corroborating_source_works_on_unpromoted_candidate(session):
    source = make_source(session, "broker_sourced")
    candidate = make_candidate(session, source)
    assert candidate.verification_status is None  # not yet set by anything

    add_corroborating_source(
        session, candidate, source_type="broker_social", evidence_date=date(2026, 1, 1),
        broker_identifier="original_post",
    )
    add_corroborating_source(
        session, candidate, source_type="trade_press", evidence_date=date(2026, 2, 1),
        url_or_reference="https://theadviser.com.au/example",
    )

    assert candidate.verification_status == "corroborated"
    persisted = session.scalars(
        select(CorroboratingSource).where(CorroboratingSource.candidate_rule_id == candidate.id)
    ).all()
    assert len(persisted) == 2
    assert all(p.production_rule_id is None for p in persisted)


def test_candidate_corroboration_carries_through_promotion(session):
    source = make_source(session, "broker_sourced")
    candidate = make_candidate(session, source)
    add_corroborating_source(session, candidate, source_type="broker_social", evidence_date=date(2026, 1, 1))
    add_corroborating_source(session, candidate, source_type="trade_press", evidence_date=date(2026, 2, 1))
    assert candidate.verification_status == "corroborated"

    production_rule = promote(session, candidate, conflict=None)

    assert production_rule.verification_status == "corroborated"


def test_calculator_probing_lender_official_candidate_is_gate_eligible(session):
    source = make_source(session, "lender_official")
    candidate = make_candidate(
        session, source, source_tier="lender_official", source_type="calculator_probing",
        confidence="official_document",
    )

    add_corroborating_source(
        session, candidate, source_type="calculator_probing", evidence_date=date(2026, 1, 1),
        broker_identifier="probe_session_1",
    )
    add_corroborating_source(
        session, candidate, source_type="calculator_probing", evidence_date=date(2026, 2, 1),
        broker_identifier="probe_session_2",
    )
    assert candidate.verification_status == "corroborated"


# --- query_rule() confidence_warning surfacing ---


def test_query_rule_surfaces_confidence_warning_for_needs_corroboration(session):
    source = make_source(session, "broker_sourced")
    candidate = make_candidate(session, source)
    promote(session, candidate, conflict=None)

    result = query_rule(session, lender="CBA", debt_type="HECS_HELP", years_remaining=0.5)

    assert result.found is True
    assert result.match.verification_status == "needs_corroboration"
    assert result.match.confidence_warning is not None
    assert "UNVERIFIED" in result.match.confidence_warning
    # not suppressed -- the actual effect is still returned
    assert result.match.effect["treatment"] == "excluded_from_serviceability"


def test_query_rule_no_warning_once_corroborated(session):
    source = make_source(session, "broker_sourced")
    candidate = make_candidate(session, source)
    rule = promote(session, candidate, conflict=None)
    add_corroborating_source(session, rule, source_type="broker_social", evidence_date=date(2026, 1, 1))
    add_corroborating_source(session, rule, source_type="trade_press", evidence_date=date(2026, 2, 1))

    result = query_rule(session, lender="CBA", debt_type="HECS_HELP", years_remaining=0.5)

    assert result.match.verification_status == "corroborated"
    assert result.match.confidence_warning is None


def test_query_rule_no_warning_for_official_document_tier(session):
    source = make_source(session, "lender_official")
    candidate = make_candidate(session, source, source_tier="lender_official", confidence="official_document")
    promote(session, candidate, conflict=None)

    result = query_rule(session, lender="CBA", debt_type="HECS_HELP", years_remaining=0.5)

    assert result.match.verification_status is None
    assert result.match.confidence_warning is None
