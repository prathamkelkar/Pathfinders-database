"""
Layer 3 corroboration gate -- CONTEXT.md section 9c.

"A single Reddit post, blog mention, or anecdote is a *lead*, not a fact, no
matter how specific or plausible it sounds." A broker_sourced production rule
starts at verification_status="needs_corroboration" and stays there until an
independent second source is registered (-> "corroborated") or a broker
directly confirms that specific candidate (-> "directly_confirmed", the
strongest tier, and it overrides plain corroboration).

This module owns the ONLY path that's allowed to move a rule out of
needs_corroboration -- promotion (ingestion/review_candidates.py) always sets
needs_corroboration for broker_sourced rules and nothing else, deliberately,
so upgrading verification status is always a distinct, explicit, auditable
step, not something that happens as a side effect of promotion.
"""

from datetime import date

from sqlalchemy.orm import Session

from db.models import CandidateRule, CorroboratingSource, LAYER3_SOURCE_TYPES, ProductionRule

Rule = CandidateRule | ProductionRule


def compute_verification_status(sources: list[CorroboratingSource]) -> str:
    """
    Pure function: given ALL corroborating-source entries on file for a rule
    (including the original lead -- see CorroboratingSource's docstring), derive
    what verification_status should be.

    - Any entry with is_direct_confirmation=True -> "directly_confirmed" (9c's
      strongest tier; a direct confirmation of THIS candidate settles it even if
      no other independent source exists).
    - Otherwise, "corroborated" as soon as any two entries are independent of each
      other: different source_type, OR BOTH have a known broker_identifier and
      those differ. Same source_type + same broker_identifier is the same lead
      repeated, not corroboration. If either side's broker is unknown, that pair
      can only become independent via source_type -- we never assume two entries
      are different brokers just because one side happens to be unnamed (that
      would make the result depend on which entry happens to have a name, not on
      the actual evidence).
    - Otherwise "needs_corroboration" -- covers zero or exactly one source on file.
    """
    if any(s.is_direct_confirmation for s in sources):
        return "directly_confirmed"

    for i in range(len(sources)):
        for j in range(i + 1, len(sources)):
            a, b = sources[i], sources[j]
            different_known_brokers = (
                a.broker_identifier is not None and b.broker_identifier is not None and a.broker_identifier != b.broker_identifier
            )
            if a.source_type != b.source_type or different_known_brokers:
                return "corroborated"

    return "needs_corroboration"


def add_corroborating_source(
    session: Session,
    rule: Rule,
    *,
    source_type: str,
    evidence_date: date,
    url_or_reference: str | None = None,
    broker_identifier: str | None = None,
    is_direct_confirmation: bool = False,
    note: str | None = None,
) -> Rule:
    """
    Register one piece of evidence for a Layer 3 rule -- a CandidateRule (most
    corroboration happens pre-promotion, when a human finds a second source
    before ever promoting the candidate) or an already-promoted ProductionRule
    -- and recompute its verification_status from the full evidence list.

    Only valid for source_tier="broker_sourced" rules, or source_tier=
    "lender_official" rules whose source_type is "calculator_probing" (9b.5 --
    it's the lender's own tool, but the inferred rule behind the numbers is
    still just as speculative as a broker tip). This gate doesn't apply to
    rules whose authority already comes from a stated official document.
    """
    is_layer3_eligible = rule.source_tier == "broker_sourced" or (
        rule.source_tier == "lender_official" and rule.source_type == "calculator_probing"
    )
    if not is_layer3_eligible:
        raise ValueError(
            f"The Layer 3 corroboration gate only applies to broker_sourced rules (or "
            f"lender_official calculator-probing rules); {type(rule).__name__} id={rule.id} has "
            f"source_tier={rule.source_tier!r}, source_type={rule.source_type!r}."
        )
    if source_type not in LAYER3_SOURCE_TYPES:
        raise ValueError(f"source_type must be one of {LAYER3_SOURCE_TYPES}, got {source_type!r}")

    entry = CorroboratingSource(
        source_type=source_type,
        url_or_reference=url_or_reference,
        broker_identifier=broker_identifier,
        date=evidence_date,
        is_direct_confirmation=is_direct_confirmation,
        note=note,
    )
    # Appending via the relationship (rather than setting the FK directly) forces
    # SQLAlchemy to load any existing entries first, so the in-memory collection
    # compute_verification_status() reads below is always complete and correctly
    # ordered, regardless of what the caller had loaded.
    rule.corroborating_sources.append(entry)
    session.add(entry)
    session.flush()

    rule.verification_status = compute_verification_status(rule.corroborating_sources)
    session.commit()
    return rule
