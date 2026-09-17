from datetime import date

import pytest
import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from db.models import Base, CandidateRule, Source
from ingestion.layer3_intake import load_layer3_file


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


def write_yaml(tmp_path, data: dict):
    path = tmp_path / "intake.yaml"
    path.write_text(yaml.dump(data))
    return path


def test_intake_creates_broker_sourced_source_with_no_url(session, tmp_path):
    path = write_yaml(
        tmp_path,
        {
            "sources": [
                {
                    "lender": "CBA",
                    "retrieved_date": "2026-09-18",
                    "note": "Broker-sourced note.",
                    "rules": [
                        {
                            "debt_type": "HECS_HELP",
                            "rule_description": "Excluded under 1 year.",
                            "conditions": {"years_remaining_max": 1},
                            "effect": {"treatment": "excluded_from_serviceability"},
                            "confidence": "corroborated_broker_source",
                        }
                    ],
                }
            ]
        },
    )

    load_layer3_file(session, path)

    source = session.scalars(select(Source).where(Source.entity_name == "CBA")).one()
    assert source.source_tier == "broker_sourced"
    assert source.url is None
    assert source.raw_content == "Broker-sourced note."
    assert source.retrieved_date == date(2026, 9, 18)


def test_intake_inserts_candidate_rules_not_production_rules(session, tmp_path):
    path = write_yaml(
        tmp_path,
        {
            "sources": [
                {
                    "lender": "CBA",
                    "note": "note",
                    "rules": [
                        {
                            "debt_type": "HECS_HELP",
                            "rule_description": "desc",
                            "conditions": {"years_remaining_max": 1},
                            "effect": {"treatment": "excluded_from_serviceability"},
                            "confidence": "single_anecdotal_source",
                        }
                    ],
                }
            ]
        },
    )

    inserted = load_layer3_file(session, path)

    assert len(inserted) == 1
    candidate = inserted[0]
    assert candidate.lender == "CBA"
    assert candidate.debt_type == "HECS_HELP"
    assert candidate.description == "desc"
    assert candidate.source_tier == "broker_sourced"
    assert candidate.confidence == "single_anecdotal_source"
    assert candidate.extraction_prompt_version is None
    assert candidate.promoted_to_production_rule_id is None

    from db.models import ProductionRule

    assert session.scalars(select(ProductionRule)).all() == []


def test_intake_rejects_invalid_confidence(session, tmp_path):
    path = write_yaml(
        tmp_path,
        {
            "sources": [
                {
                    "lender": "CBA",
                    "note": "note",
                    "rules": [
                        {
                            "debt_type": "HECS_HELP",
                            "conditions": {},
                            "effect": {},
                            "confidence": "totally_sure",  # invalid
                        }
                    ],
                }
            ]
        },
    )

    with pytest.raises(ValueError, match="confidence must be one of"):
        load_layer3_file(session, path)


def test_intake_defaults_effective_from_to_retrieved_date(session, tmp_path):
    path = write_yaml(
        tmp_path,
        {
            "sources": [
                {
                    "lender": "CBA",
                    "retrieved_date": "2026-01-15",
                    "note": "note",
                    "rules": [
                        {
                            "debt_type": "HECS_HELP",
                            "conditions": {},
                            "effect": {},
                            "confidence": "single_anecdotal_source",
                        }
                    ],
                }
            ]
        },
    )

    inserted = load_layer3_file(session, path)
    assert inserted[0].effective_from == date(2026, 1, 15)


def test_multiple_rules_share_one_source(session, tmp_path):
    path = write_yaml(
        tmp_path,
        {
            "sources": [
                {
                    "lender": "CBA",
                    "note": "one broker conversation, two related rules",
                    "rules": [
                        {
                            "debt_type": "HECS_HELP",
                            "conditions": {"years_remaining_max": 1},
                            "effect": {"treatment": "excluded_from_serviceability"},
                            "confidence": "corroborated_broker_source",
                        },
                        {
                            "debt_type": "HECS_HELP",
                            "conditions": {"years_remaining_min": 1, "years_remaining_max": 5},
                            "effect": {"treatment": "reduced_buffer", "buffer_pct": 1.0},
                            "confidence": "corroborated_broker_source",
                        },
                    ],
                }
            ]
        },
    )

    inserted = load_layer3_file(session, path)

    assert len(inserted) == 2
    assert inserted[0].source_id == inserted[1].source_id
    assert session.scalars(select(Source)).all().__len__() == 1


def test_real_cba_hecs_intake_file_loads_correctly(session):
    from pathlib import Path

    path = Path(__file__).parent.parent / "ingestion" / "layer3_rules" / "cba_hecs_serviceability.yaml"
    inserted = load_layer3_file(session, path)

    assert len(inserted) == 2
    exclusion = next(c for c in inserted if c.conditions.get("years_remaining_max") == 1 and "years_remaining_min" not in c.conditions)
    buffer_rule = next(c for c in inserted if c.conditions.get("years_remaining_min") == 1)

    assert exclusion.effect["treatment"] == "excluded_from_serviceability"
    assert buffer_rule.effect["treatment"] == "reduced_buffer"
    assert buffer_rule.effect["buffer_pct"] == 1.0
    assert all(c.confidence == "corroborated_broker_source" for c in inserted)
    assert all(c.source_tier == "broker_sourced" for c in inserted)
