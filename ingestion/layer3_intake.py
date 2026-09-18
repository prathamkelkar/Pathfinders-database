"""
Manual intake path for Layer 3 rules: undocumented/inferred lender policy with
no public source document -- the highest-value, hardest-to-replicate layer
per CONTEXT.md section 2.

A human fills in a YAML file (see ingestion/layer3_rules/TEMPLATE.yaml)
describing what they know and where it came from. This script inserts it into
candidate_rules exactly like the LLM extraction pipeline does -- it does NOT
write to production_rules directly. A human still has to run
ingestion/review_candidates.py to promote it, same as any other candidate.
"""

import argparse
from datetime import date, datetime
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from db.models import LAYER3_SOURCE_TYPES, CandidateRule, Source
from db.session import get_session

VALID_CONFIDENCE = {"single_anecdotal_source", "corroborated_broker_source"}


def _parse_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def load_layer3_file(session: Session, path: Path) -> list[CandidateRule]:
    data = yaml.safe_load(path.read_text())
    inserted: list[CandidateRule] = []

    for source_block in data.get("sources", []):
        lender = source_block["lender"]
        note = source_block["note"]
        retrieved_date = _parse_date(source_block.get("retrieved_date")) or date.today()

        # Optional: tags the lead with one of CONTEXT.md 9b's evidence categories.
        # CorroboratingSource (db/models.py) only links to already-PROMOTED
        # production_rules -- candidates have no structured place for this yet --
        # so it's recorded here in raw_content for auditability rather than left
        # off entirely. A public brokerage blog/article is "broker_content_marketing"
        # (9b category 1); an unverified forum/social post is "broker_social" (9b.3).
        source_type = source_block.get("source_type")
        if source_type is not None and source_type not in LAYER3_SOURCE_TYPES:
            raise ValueError(f"source_type must be one of {LAYER3_SOURCE_TYPES}, got {source_type!r}")
        url = source_block.get("url")
        note_with_provenance = note
        if source_type is not None or url is not None:
            tag_line = f"[source_type={source_type or 'unknown'}" + (f" url={url}" if url else "") + "]\n"
            note_with_provenance = tag_line + note

        source = Source(
            entity_name=lender,
            parent_entity=source_block.get("parent_entity"),
            source_tier="broker_sourced",
            url=url,  # usually None -- Layer 3 rules typically have no public document --
            # but a brokerage blog post IS itself a public, linkable URL, so allow one here.
            retrieved_date=retrieved_date,
            raw_content=note_with_provenance,
        )
        session.add(source)
        session.flush()

        for rule in source_block.get("rules", []):
            confidence = rule["confidence"]
            if confidence not in VALID_CONFIDENCE:
                raise ValueError(
                    f"confidence must be one of {sorted(VALID_CONFIDENCE)}, got "
                    f"{confidence!r} for lender={lender} debt_type={rule.get('debt_type')}"
                )

            candidate = CandidateRule(
                lender=lender,
                debt_type=rule["debt_type"],
                description=rule.get("rule_description"),
                conditions=rule.get("conditions") or {},
                effect=rule.get("effect") or {},
                source_tier="broker_sourced",
                effective_from=_parse_date(rule.get("effective_from")) or retrieved_date,
                effective_to=_parse_date(rule.get("effective_to")),
                confidence=confidence,
                conflicting_sources=False,
                extraction_prompt_version=None,  # manually authored, not LLM-extracted
                # CONTEXT.md 9c: every broker_sourced candidate starts unverified,
                # visible from intake, not just once promoted -- same default
                # review_candidates.py's promote() applies at promotion time.
                verification_status="needs_corroboration",
                source_id=source.id,
            )
            session.add(candidate)
            inserted.append(candidate)

    session.flush()
    session.commit()
    return inserted


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manually intake Layer 3 (broker-sourced) rules from a YAML file.")
    parser.add_argument("path", type=Path, help="Path to a Layer 3 intake YAML file.")
    args = parser.parse_args()

    session = get_session()
    inserted = load_layer3_file(session, args.path)
    print(f"Inserted {len(inserted)} candidate_rule(s) from {args.path}.")
    print("Run `python3 -m ingestion.review_candidates` to review and promote them.")
    for c in inserted:
        print(f"  id={c.id} lender={c.lender} debt_type={c.debt_type} confidence={c.confidence}")
