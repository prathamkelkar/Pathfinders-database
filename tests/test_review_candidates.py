from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from db.models import Base, CandidateRule, ProductionRule, Source
from ingestion.review_candidates import find_conflict, run_review


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
        url="https://example.com/cba.pdf",
        retrieved_date=date(2026, 9, 17),
        raw_content="...",
    )
    session.add(src)
    session.flush()
    return src


def make_candidate(session, source, **overrides):
    defaults = dict(
        lender="CBA",
        debt_type="home_loan",
        conditions={"feature": "rate_lock"},
        effect={"fee_charged": True},
        source_tier="lender_official",
        effective_from=date(2020, 1, 1),  # deliberately different from source.retrieved_date
        confidence="official_document",
        conflicting_sources=False,
        extraction_prompt_version="1",
        source_id=source.id,
    )
    defaults.update(overrides)
    candidate = CandidateRule(**defaults)
    session.add(candidate)
    session.flush()
    return candidate


def test_no_candidates_reports_nothing(session, capsys):
    approved, skipped = run_review(session, input_func=lambda prompt: "y")
    assert (approved, skipped) == (0, 0)
    assert "No unpromoted candidate_rules" in capsys.readouterr().out


def test_approve_promotes_with_source_retrieved_date_as_effective_from(session, source):
    candidate = make_candidate(session, source)

    approved, skipped = run_review(session, input_func=lambda prompt: "y")

    assert (approved, skipped) == (1, 0)
    production_rules = session.scalars(select(ProductionRule)).all()
    assert len(production_rules) == 1
    promoted = production_rules[0]
    # effective_from comes from source.retrieved_date, NOT the candidate's own
    # effective_from (2020-01-01) -- this is the documented design choice.
    assert promoted.effective_from == source.retrieved_date
    assert promoted.effective_from != candidate.effective_from
    assert promoted.lender == "CBA"
    assert promoted.conditions == {"feature": "rate_lock"}
    assert promoted.promoted_from_candidate_rule_id == candidate.id
    assert promoted.conflicting_sources is False
    assert promoted.conflict_note is None

    session.refresh(candidate)
    assert candidate.promoted_to_production_rule_id == promoted.id


def test_promote_carries_plausibility_check_across_unchanged(session, source):
    fake_check = {"plausible": True, "claimed_swing_pct": 22.4, "expected_swing_pct": 20.9, "explanation": "..."}
    candidate = make_candidate(session, source, plausibility_check=fake_check)

    run_review(session, input_func=lambda prompt: "y")

    promoted = session.scalars(select(ProductionRule)).one()
    assert promoted.plausibility_check == fake_check


def test_skip_does_not_promote(session, source):
    candidate = make_candidate(session, source)

    approved, skipped = run_review(session, input_func=lambda prompt: "n")

    assert (approved, skipped) == (0, 1)
    assert session.scalars(select(ProductionRule)).all() == []
    session.refresh(candidate)
    assert candidate.promoted_to_production_rule_id is None


def test_quit_stops_early_and_keeps_prior_approvals(session, source):
    make_candidate(session, source, debt_type="home_loan", conditions={"a": 1})
    make_candidate(session, source, debt_type="personal_loan", conditions={"b": 2})

    responses = iter(["y", "q"])
    approved, skipped = run_review(session, input_func=lambda prompt: next(responses))

    assert (approved, skipped) == (1, 0)
    assert len(session.scalars(select(ProductionRule)).all()) == 1


def test_approve_all_skips_prompting_for_non_conflicting_candidates(session, source):
    make_candidate(session, source, debt_type="home_loan", conditions={"a": 1})
    make_candidate(session, source, debt_type="personal_loan", conditions={"b": 2})

    def fail_if_called(prompt):
        raise AssertionError("input_func should not be called for non-conflicting candidates under --approve-all")

    approved, skipped = run_review(session, approve_all=True, input_func=fail_if_called)

    assert (approved, skipped) == (2, 0)


def test_conflict_detected_against_existing_production_rule(session, source):
    existing = ProductionRule(
        lender="CBA",
        debt_type="home_loan",
        conditions={"feature": "rate_lock"},
        effect={"fee_charged": False},  # disagrees with the candidate below
        source_tier="lender_official",
        effective_from=date(2025, 1, 1),
        confidence="official_document",
        conflicting_sources=False,
        source_id=source.id,
    )
    session.add(existing)
    session.flush()

    candidate = make_candidate(session, source, effect={"fee_charged": True})

    conflict = find_conflict(session, candidate)
    assert conflict is not None
    assert conflict.id == existing.id


def test_conflict_requires_explicit_approval_even_under_approve_all(session, source):
    existing = ProductionRule(
        lender="CBA",
        debt_type="home_loan",
        conditions={"feature": "rate_lock"},
        effect={"fee_charged": False},
        source_tier="lender_official",
        effective_from=date(2025, 1, 1),
        confidence="official_document",
        conflicting_sources=False,
        source_id=source.id,
    )
    session.add(existing)
    session.flush()
    candidate = make_candidate(session, source, effect={"fee_charged": True})

    prompts = []

    def record_and_reject(prompt):
        prompts.append(prompt)
        return "n"

    approved, skipped = run_review(session, approve_all=True, input_func=record_and_reject)

    # --approve-all must NOT silently approve a conflicting candidate
    assert (approved, skipped) == (0, 1)
    assert len(prompts) == 1
    assert "conflict" in prompts[0].lower()


def test_approving_a_conflict_records_conflicting_sources_and_note(session, source):
    existing = ProductionRule(
        lender="CBA",
        debt_type="home_loan",
        conditions={"feature": "rate_lock"},
        effect={"fee_charged": False},
        source_tier="lender_official",
        effective_from=date(2025, 1, 1),
        confidence="official_document",
        conflicting_sources=False,
        source_id=source.id,
    )
    session.add(existing)
    session.flush()
    make_candidate(session, source, effect={"fee_charged": True})

    run_review(session, input_func=lambda prompt: "y")

    promoted = session.scalars(
        select(ProductionRule).where(ProductionRule.id != existing.id)
    ).one()
    assert promoted.conflicting_sources is True
    assert str(existing.id) in promoted.conflict_note


def test_already_promoted_candidates_are_not_listed_again(session, source):
    make_candidate(session, source)

    run_review(session, input_func=lambda prompt: "y")
    approved, skipped = run_review(session, input_func=lambda prompt: "y")

    assert (approved, skipped) == (0, 0)
