"""
Review script: surfaces unpromoted candidate_rules for manual review, flags
same-source_tier conflicts against existing production_rules, and lets a human
approve or reject each one via a y/n CLI prompt (or bulk-approve with
--approve-all). Deliberately just a script, not a UI -- at ~15 sources this is
enough (CONTEXT.md section 6).
"""

import argparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import CandidateRule, ProductionRule
from db.session import get_session


def find_conflict(session: Session, candidate: CandidateRule) -> ProductionRule | None:
    """
    A same-tier conflict is: an existing production_rule for the same lender,
    debt_type, source_tier, AND policy_area, with identical conditions but a
    different effect. The policy_area match matters -- without it, a break_cost
    rule and a serviceability rule that happen to share identical conditions
    (e.g. both {"loan_type": "Fixed Rate"}) would be flagged as conflicting with
    each other despite being about entirely different things. Deliberately
    conservative rather than exhaustive -- section 6 asks to keep this simple;
    anything subtler is still visible in the printed summary for the human to judge.
    """
    existing = session.scalars(
        select(ProductionRule).where(
            ProductionRule.lender == candidate.lender,
            ProductionRule.debt_type == candidate.debt_type,
            ProductionRule.source_tier == candidate.source_tier,
            ProductionRule.policy_area == candidate.policy_area,
        )
    ).all()
    for rule in existing:
        if rule.conditions == candidate.conditions and rule.effect != candidate.effect:
            return rule
    return None


def print_candidate(index: int, total: int, candidate: CandidateRule, conflict: ProductionRule | None) -> None:
    source = candidate.source
    print(f"\n[{index}/{total}] candidate_rule id={candidate.id}")
    print(f"  lender:      {candidate.lender}")
    print(f"  debt_type:   {candidate.debt_type}")
    print(f"  source_tier: {candidate.source_tier}")
    print(f"  confidence:  {candidate.confidence}")
    print(f"  conditions:  {candidate.conditions}")
    print(f"  effect:      {candidate.effect}")
    print(f"  effective_from will be set to source.retrieved_date: {source.retrieved_date}")
    print(f"  source:      {source.url} (retrieved {source.retrieved_date})")
    print(f"  extraction_prompt_version: {candidate.extraction_prompt_version}")
    if candidate.plausibility_check is not None:
        pc = candidate.plausibility_check
        print(
            f"  plausibility check (supporting signal only, does not gate approval): "
            f"plausible={pc.get('plausible')} claimed={pc.get('claimed_swing_pct')}% "
            f"expected={pc.get('expected_swing_pct')}%"
        )
        print(f"    {pc.get('explanation')}")
    if conflict is not None:
        print(f"  !! CONFLICT with production_rule id={conflict.id} at the same source_tier ({candidate.source_tier}):")
        print(f"     existing effect: {conflict.effect}")
        print(f"     proposed effect: {candidate.effect}")


def promote(session: Session, candidate: CandidateRule, conflict: ProductionRule | None) -> ProductionRule:
    production_rule = ProductionRule(
        lender=candidate.lender,
        debt_type=candidate.debt_type,
        policy_area=candidate.policy_area,
        conditions=candidate.conditions,
        effect=candidate.effect,
        source_tier=candidate.source_tier,
        legislation_status=candidate.legislation_status,
        enacted_date=candidate.enacted_date,
        commencement_date=candidate.commencement_date,
        # Deliberately the source's retrieved_date, not whatever effective_from
        # the candidate carried -- this records when we confirmed the rule to be
        # true, rather than trusting a document-stated date the LLM may have
        # mis-attributed to the wrong clause.
        effective_from=candidate.source.retrieved_date,
        effective_to=candidate.effective_to,
        confidence=candidate.confidence,
        conflicting_sources=conflict is not None,
        conflict_note=(
            f"Conflicts with production_rule id={conflict.id} at source_tier={candidate.source_tier}: "
            f"existing effect={conflict.effect!r} vs proposed effect={candidate.effect!r}"
        )
        if conflict is not None
        else None,
        extraction_prompt_version=candidate.extraction_prompt_version,
        # Layer 3 corroboration gate (CONTEXT.md 9c): normally carried through
        # UNCHANGED from the candidate -- the candidate-intake path
        # (layer3_intake.py, calculator_probe_intake.py) is what decides whether a
        # rule starts at needs_corroboration, since lender_official calculator-probe
        # candidates (9b.5) need this too, not just broker_sourced ones. The `or`
        # fallback is a safety net for any broker_sourced candidate that reached
        # promotion without it already set. Promotion never sets this to anything
        # PAST needs_corroboration -- that requires a separate, explicit call to
        # ingestion.layer3_verification.add_corroborating_source().
        verification_status=candidate.verification_status
        or ("needs_corroboration" if candidate.source_tier == "broker_sourced" else None),
        source_type=candidate.source_type,
        # Supporting signal only (9d) -- carried through for the permanent record,
        # never consulted by promotion logic itself.
        plausibility_check=candidate.plausibility_check,
        source_id=candidate.source_id,
        promoted_from_candidate_rule_id=candidate.id,
    )
    session.add(production_rule)
    session.flush()
    candidate.promoted_to_production_rule_id = production_rule.id
    session.commit()
    return production_rule


def run_review(session: Session, *, approve_all: bool = False, input_func=input) -> tuple[int, int]:
    """Returns (approved_count, skipped_count)."""
    candidates = session.scalars(
        select(CandidateRule).where(CandidateRule.promoted_to_production_rule_id.is_(None))
    ).all()

    if not candidates:
        print("No unpromoted candidate_rules to review.")
        return (0, 0)

    print(f"{len(candidates)} unpromoted candidate_rule(s) to review.")

    approved = 0
    skipped = 0
    for i, candidate in enumerate(candidates, start=1):
        conflict = find_conflict(session, candidate)
        print_candidate(i, len(candidates), candidate, conflict)

        if conflict is not None:
            # Conflicts always require an explicit human decision, even under
            # --approve-all -- that's the entire point of flagging them (4c/4f).
            decision = input_func("  Approve despite conflict? [y/n/q to quit] ").strip().lower()
        elif approve_all:
            decision = "y"
        else:
            decision = input_func("  Approve? [y/n/q to quit] ").strip().lower()

        if decision == "q":
            print("\nQuitting review early.")
            break
        if decision == "y":
            production_rule = promote(session, candidate, conflict)
            print(f"  -> promoted to production_rule id={production_rule.id}")
            approved += 1
        else:
            print("  -> skipped")
            skipped += 1

    print(f"\nDone. Approved {approved}, skipped {skipped}.")
    return (approved, skipped)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Review candidate_rules for promotion to production_rules.")
    parser.add_argument(
        "--approve-all",
        action="store_true",
        help="Approve every non-conflicting unpromoted candidate without prompting. "
        "Candidates that conflict with an existing production_rule still require "
        "an explicit y/n even with this flag set.",
    )
    args = parser.parse_args()

    run_review(get_session(), approve_all=args.approve_all)
