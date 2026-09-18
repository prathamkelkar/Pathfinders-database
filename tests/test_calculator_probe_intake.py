from datetime import date

import pytest
import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from db.models import Base, CandidateRule, ProductionRule, Source
from ingestion.calculator_probe_intake import load_calculator_probe_file
from ingestion.review_candidates import promote


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


def write_probe_yaml(tmp_path, data: dict, name="probe.yaml"):
    path = tmp_path / name
    path.write_text(yaml.dump(data))
    return path


def base_probe_data(**overrides):
    data = {
        "lender": "CBA",
        "calculator_url": "https://example.com/calc",
        "probe_session_date": "2026-09-19",
        "baseline": {"income_aud": 140000, "other_debts_aud": 5000, "loan_term_years": 30},
        "runs": [
            {
                "label": "no_hecs",
                "hecs_balance_aud": 0,
                "hecs_years_remaining": None,
                "borrowing_power_result_aud": 610000,
                "screenshot_path": "sources/calculator_probes/cba/no_hecs.png",
            },
            {
                "label": "small_1yr",
                "hecs_balance_aud": 5000,
                "hecs_years_remaining": 1,
                "borrowing_power_result_aud": 605000,
                "screenshot_path": "sources/calculator_probes/cba/small_1yr.png",
            },
        ],
    }
    data.update(overrides)
    return data


def test_source_is_lender_official_not_broker_sourced(session, tmp_path):
    path = write_probe_yaml(tmp_path, base_probe_data())
    load_calculator_probe_file(session, path)

    source = session.scalars(select(Source).where(Source.entity_name == "CBA")).one()
    assert source.source_tier == "lender_official"
    assert source.url == "https://example.com/calc"
    assert source.retrieved_date == date(2026, 9, 19)


def test_candidates_tagged_correctly(session, tmp_path):
    path = write_probe_yaml(tmp_path, base_probe_data())
    inserted = load_calculator_probe_file(session, path)

    assert len(inserted) == 2
    for c in inserted:
        assert c.lender == "CBA"
        assert c.debt_type == "HECS_HELP"
        assert c.source_tier == "lender_official"
        assert c.source_type == "calculator_probing"
        assert c.confidence == "official_document"
        assert c.verification_status == "needs_corroboration"  # default despite lender_official tier
        assert c.extraction_prompt_version is None


def test_conditions_and_effect_capture_the_probe_point(session, tmp_path):
    path = write_probe_yaml(tmp_path, base_probe_data())
    inserted = load_calculator_probe_file(session, path)

    no_hecs, small_1yr = inserted
    assert no_hecs.conditions == {
        "income_aud": 140000, "other_debts_aud": 5000, "loan_term_years": 30,
        "hecs_balance_aud": 0, "hecs_years_remaining": None,
    }
    assert no_hecs.effect["borrowing_power_aud"] == 610000
    assert small_1yr.effect["borrowing_power_aud"] == 605000
    assert small_1yr.effect["screenshot_path"] == "sources/calculator_probes/cba/small_1yr.png"


def test_never_writes_to_production_rules(session, tmp_path):
    path = write_probe_yaml(tmp_path, base_probe_data())
    load_calculator_probe_file(session, path)
    assert session.scalars(select(ProductionRule)).all() == []


def test_promotion_preserves_needs_corroboration_despite_lender_official_tier(session, tmp_path):
    """
    This is the exact bug this task's schema change had to fix: promote() used to
    reset verification_status to None for anything that wasn't broker_sourced.
    """
    path = write_probe_yaml(tmp_path, base_probe_data())
    inserted = load_calculator_probe_file(session, path)
    candidate = inserted[0]

    production_rule = promote(session, candidate, conflict=None)

    assert production_rule.source_tier == "lender_official"
    assert production_rule.verification_status == "needs_corroboration"
    assert production_rule.source_type == "calculator_probing"


def test_independent_consistent_probe_gets_flagged_for_expedited_review(session, tmp_path):
    first_path = write_probe_yaml(tmp_path, base_probe_data(probe_session_date="2026-09-01"), name="first.yaml")
    load_calculator_probe_file(session, first_path)

    # same lender, same exact input point (no_hecs run), same output, different session date
    second_path = write_probe_yaml(tmp_path, base_probe_data(probe_session_date="2026-09-15"), name="second.yaml")
    second_inserted = load_calculator_probe_file(session, second_path)

    no_hecs_run = next(c for c in second_inserted if c.effect["probe_label"] == "no_hecs")
    assert "EXPEDITED REVIEW" in no_hecs_run.description
    # still needs_corroboration -- flagging is not the same as upgrading status or auto-promoting
    assert no_hecs_run.verification_status == "needs_corroboration"


def test_divergent_independent_probe_does_not_get_flagged(session, tmp_path):
    first_path = write_probe_yaml(tmp_path, base_probe_data(probe_session_date="2026-09-01"), name="first.yaml")
    load_calculator_probe_file(session, first_path)

    diverging = base_probe_data(probe_session_date="2026-09-15")
    diverging["runs"][0]["borrowing_power_result_aud"] = 900000  # wildly different from first session's 610000
    second_path = write_probe_yaml(tmp_path, diverging, name="second.yaml")
    second_inserted = load_calculator_probe_file(session, second_path)

    no_hecs_run = next(c for c in second_inserted if c.effect["probe_label"] == "no_hecs")
    assert "EXPEDITED REVIEW" not in no_hecs_run.description


def test_different_lender_does_not_trigger_false_consistency_match(session, tmp_path):
    first_path = write_probe_yaml(tmp_path, base_probe_data(probe_session_date="2026-09-01"), name="first.yaml")
    load_calculator_probe_file(session, first_path)

    other_lender = base_probe_data(lender="Westpac", probe_session_date="2026-09-15")
    second_path = write_probe_yaml(tmp_path, other_lender, name="second.yaml")
    second_inserted = load_calculator_probe_file(session, second_path)

    no_hecs_run = next(c for c in second_inserted if c.effect["probe_label"] == "no_hecs")
    assert "EXPEDITED REVIEW" not in no_hecs_run.description
