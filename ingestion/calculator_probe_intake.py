"""
Intake path for Layer 3 calculator-probe results -- CONTEXT.md 9b.5: "where a
lender's public borrowing-power calculator accepts HECS/HELP as a distinct
input, systematically varying that input while holding other variables
constant produces first-party empirical evidence of that lender's actual
policy, sourced from the lender's own tool rather than secondhand description."

IMPORTANT -- this project has no browser-automation tool (no Playwright/
Puppeteer/screenshot capability, only static HTTP fetching). This script does
NOT drive a browser or submit anything itself. A human runs each calculator
manually and records the input/output pairs plus a saved screenshot into a
YAML file (see ingestion/calculator_probes/TEMPLATE.yaml); this script loads
that YAML and inserts the results as candidate_rules. If you DO end up
automating the actual fetching yourself, apply the exact same rate-limiting and
robots.txt/ToS discipline as ingestion/scrapers/base.py (MIN_DELAY_SECONDS,
checking robots.txt before each request) -- there is no exemption for
calculator probing just because it looks like normal browsing.

Why source_tier="lender_official" but still gated like Layer 3: it IS the
lender's own tool, not an anecdote -- but the specific serviceability RULE
behind the numbers is inferred by us from input/output behaviour, not stated
anywhere. That inference is exactly as speculative as a broker's tip, so it
goes through the same verification_status gate (db/models.py, 9c) despite
being a higher source_tier than broker_sourced. Never auto-promoted: even when
a pattern is consistent across independent probe sessions, this only flags the
candidate for expedited human review (ingestion/review_candidates.py), never
skips review.
"""

import argparse
from datetime import date, datetime
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import CandidateRule, Source
from db.session import get_session

SOURCE_TYPE = "calculator_probing"
CONSISTENCY_TOLERANCE_PCT = 5.0


def _parse_date(value) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _check_consistency(
    session: Session, *, lender: str, debt_type: str, conditions: dict, borrowing_power_aud: float, exclude_source_id: int
) -> list[CandidateRule]:
    """
    Finds prior calculator-probe candidates for the exact same lender/debt_type/
    conditions (i.e. the same point in the probe matrix) from a DIFFERENT probe
    session (different source_id -- CONTEXT.md 9c's "independent" run), whose
    recorded borrowing_power_aud is within CONSISTENCY_TOLERANCE_PCT of this one.

    Deliberately literal rather than fuzzy: "the same pattern shows up
    consistently" is read here as "the same input point produces the same output,
    on a separate occasion" -- not an attempt to detect a qualitative trend across
    different input points, which would need a much less certain heuristic.
    """
    candidates = session.scalars(
        select(CandidateRule).where(
            CandidateRule.lender == lender,
            CandidateRule.debt_type == debt_type,
            CandidateRule.source_type == SOURCE_TYPE,
            CandidateRule.source_id != exclude_source_id,
        )
    ).all()

    matches = []
    for c in candidates:
        if c.conditions != conditions:
            continue
        other_value = c.effect.get("borrowing_power_aud")
        if other_value is None or borrowing_power_aud == 0:
            continue
        divergence_pct = abs(other_value - borrowing_power_aud) / abs(borrowing_power_aud) * 100
        if divergence_pct <= CONSISTENCY_TOLERANCE_PCT:
            matches.append(c)
    return matches


def load_calculator_probe_file(session: Session, path: Path) -> list[CandidateRule]:
    data = yaml.safe_load(path.read_text())

    lender = data["lender"]
    calculator_url = data["calculator_url"]
    probe_session_date = _parse_date(data["probe_session_date"])
    baseline = data.get("baseline") or {}
    runs = data["runs"]

    note_lines = [
        f"Calculator probe session for {lender}.",
        f"Calculator URL: {calculator_url}",
        f"Session date: {probe_session_date.isoformat()}",
        f"Baseline (held constant across all runs): {baseline}",
        "Runs:",
    ]
    for run in runs:
        note_lines.append(
            f"  - {run.get('label', '(unlabelled)')}: "
            f"hecs_balance_aud={run.get('hecs_balance_aud')}, "
            f"hecs_years_remaining={run.get('hecs_years_remaining')} "
            f"-> borrowing_power_aud={run['borrowing_power_result_aud']} "
            f"(screenshot: {run.get('screenshot_path', 'none recorded')})"
        )

    source = Source(
        entity_name=lender,
        parent_entity=data.get("parent_entity"),
        source_tier="lender_official",
        url=calculator_url,
        retrieved_date=probe_session_date,
        raw_file_path=runs[0].get("screenshot_path") if runs else None,
        raw_content="\n".join(note_lines),
    )
    session.add(source)
    session.flush()

    inserted: list[CandidateRule] = []
    for run in runs:
        conditions = dict(baseline)
        conditions["hecs_balance_aud"] = run.get("hecs_balance_aud")
        conditions["hecs_years_remaining"] = run.get("hecs_years_remaining")

        borrowing_power_aud = run["borrowing_power_result_aud"]
        effect = {
            "borrowing_power_aud": borrowing_power_aud,
            "probe_label": run.get("label"),
            "screenshot_path": run.get("screenshot_path"),
        }

        description = (
            f"Calculator probe: {lender}'s borrowing-power calculator ({calculator_url}), "
            f"run {run.get('label', '')} on {probe_session_date.isoformat()}. "
            f"Baseline {baseline} with hecs_balance_aud={conditions['hecs_balance_aud']}, "
            f"hecs_years_remaining={conditions['hecs_years_remaining']} produced "
            f"borrowing_power_aud={borrowing_power_aud}."
        )

        consistent_with = _check_consistency(
            session,
            lender=lender,
            debt_type="HECS_HELP",
            conditions=conditions,
            borrowing_power_aud=borrowing_power_aud,
            exclude_source_id=source.id,
        )
        if consistent_with:
            other_ids = ", ".join(str(c.id) for c in consistent_with)
            description += (
                f" EXPEDITED REVIEW: this exact input point produced a consistent result "
                f"(within {CONSISTENCY_TOLERANCE_PCT}%) across an independent prior probe session "
                f"(candidate_rule id(s) {other_ids}) -- still needs_corroboration, not auto-promoted, "
                f"but worth reviewing sooner than a single-session probe."
            )

        candidate = CandidateRule(
            lender=lender,
            debt_type="HECS_HELP",
            description=description,
            conditions=conditions,
            effect=effect,
            source_tier="lender_official",
            source_type=SOURCE_TYPE,
            effective_from=probe_session_date,
            confidence="official_document",
            conflicting_sources=False,
            extraction_prompt_version=None,
            # Same gate as broker_sourced Layer 3 candidates (9c) despite the higher
            # source_tier -- see module docstring. Never anything but
            # needs_corroboration at intake; consistency across sessions only earns
            # an expedited-review flag above, never a status change or auto-promotion.
            verification_status="needs_corroboration",
            source_id=source.id,
        )
        session.add(candidate)
        inserted.append(candidate)

    session.flush()
    session.commit()
    return inserted


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Intake a manually-run calculator-probe session from a YAML file.")
    parser.add_argument("path", type=Path, help="Path to a calculator-probe YAML file.")
    args = parser.parse_args()

    session = get_session()
    inserted = load_calculator_probe_file(session, args.path)
    print(f"Inserted {len(inserted)} candidate_rule(s) from {args.path}.")
    print("Run `python3 -m ingestion.review_candidates` to review and promote them.")
    for c in inserted:
        flagged = "EXPEDITED REVIEW" in (c.description or "")
        print(f"  id={c.id} lender={c.lender} conditions={c.conditions} effect={c.effect}" + (" [FLAGGED]" if flagged else ""))
