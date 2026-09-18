"""
Read-only query interface against `production_rules` (section 7). Deterministic,
no live fetching, no LLM calls -- just filtering rows already in the database.
"""

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import SOURCE_TIERS, ProductionRule, Rate, Source


def _conditions_match(conditions: dict, years_remaining: float) -> bool:
    years_min = conditions.get("years_remaining_min")
    years_max = conditions.get("years_remaining_max")

    if years_min is not None and years_remaining < years_min:
        return False
    if years_max is not None and years_remaining >= years_max:
        return False
    return True


def get_serviceability_treatment(
    session: Session, *, lender: str, debt_type: str, years_remaining: float
) -> dict:
    """
    Return the `effect` dict of the production rule whose `conditions` match the given
    years_remaining, for the given lender + debt_type.

    Raises LookupError if no matching rule is found.
    """
    rules = session.scalars(
        select(ProductionRule).where(
            ProductionRule.lender == lender,
            ProductionRule.debt_type == debt_type,
        )
    ).all()

    for rule in rules:
        if _conditions_match(rule.conditions, years_remaining):
            return rule.effect

    raise LookupError(
        f"No production rule found for lender={lender!r}, debt_type={debt_type!r}, "
        f"years_remaining={years_remaining!r}"
    )


# --- General-purpose query interface (this is the one an advisory tool/agent should call) ---

# Highest authority first, per CONTEXT.md 4c. Reusing the model's own tuple keeps this
# in lockstep with the enum -- there is exactly one place that ordering is defined.
_SOURCE_TIER_RANK = {tier: i for i, tier in enumerate(SOURCE_TIERS)}


@dataclass(frozen=True)
class SourceCitation:
    """Provenance for a returned rule or rate -- CONTEXT.md 4g: nothing is returned without one."""

    source_id: int
    entity_name: str
    url: str | None
    retrieved_date: date
    source_tier: str


@dataclass(frozen=True)
class RuleMatch:
    lender: str
    debt_type: str
    conditions: dict
    effect: dict
    confidence: str
    source: SourceCitation
    effective_from: date
    effective_to: date | None
    conflicting_sources: bool
    conflict_note: str | None
    verification_status: str | None
    # Set (non-None) ONLY when verification_status == "needs_corroboration" --
    # CONTEXT.md 9c: a single-source Layer 3 lead must never be presented the same
    # way as a corroborated or official_document-tier rule. Not suppressed --
    # `effect`/`confidence`/etc. above are still populated -- just clearly flagged.
    confidence_warning: str | None


@dataclass(frozen=True)
class RateMatch:
    lender: str
    product_name: str
    rate_type: str
    rate_pct: float
    source: SourceCitation


@dataclass(frozen=True)
class RuleQueryResult:
    """
    Result of query_rule(). `found` tells you whether there's anything usable at all --
    always check it before touching `match`, since a "no data" outcome is a normal,
    expected result (CONTEXT.md: a stale or silently-wrong answer is worse than none),
    not an error condition. `conflicting_sources` tells you whether `match` is safe to
    treat as settled: when True, `conflicts` holds every disagreeing rule so the caller
    (or a human) can resolve it -- this interface never silently picks one for you.
    """

    found: bool
    lender: str
    debt_type: str
    message: str
    match: RuleMatch | None = None
    conflicting_sources: bool = False
    conflicts: list[RuleMatch] = field(default_factory=list)
    rates: list[RateMatch] = field(default_factory=list)


def _citation(source: Source) -> SourceCitation:
    return SourceCitation(
        source_id=source.id,
        entity_name=source.entity_name,
        url=source.url,
        retrieved_date=source.retrieved_date,
        source_tier=source.source_tier,
    )


def _confidence_warning(rule: ProductionRule) -> str | None:
    if rule.verification_status != "needs_corroboration":
        return None
    return (
        "UNVERIFIED INSIGHT: this rule comes from a single, uncorroborated Layer 3 "
        "source and has not been independently confirmed (CONTEXT.md 9c). Treat it as "
        "worth investigating with a broker, not as a settled fact or a basis for a "
        "specific dollar-figure recommendation."
    )


def _to_rule_match(rule: ProductionRule) -> RuleMatch:
    return RuleMatch(
        lender=rule.lender,
        debt_type=rule.debt_type,
        conditions=rule.conditions,
        effect=rule.effect,
        confidence=rule.confidence,
        source=_citation(rule.source),
        effective_from=rule.effective_from,
        effective_to=rule.effective_to,
        conflicting_sources=rule.conflicting_sources,
        conflict_note=rule.conflict_note,
        verification_status=rule.verification_status,
        confidence_warning=_confidence_warning(rule),
    )


def _conditions_satisfied(conditions: dict, params: dict) -> bool:
    """
    Generalizes the CBA-HECS-specific years_remaining check above to arbitrary
    condition keys, since a real caller may ask about any debt type, not just
    HECS-HELP:

    - For a "<name>_min" / "<name>_max" range condition, it's checked only if the
      caller supplied `<name>` in **params; if they didn't, that dimension simply
      isn't verified (the caller is asserting "I don't know/care about this", not
      "this rule can't apply"). `_max` is an exclusive upper bound, matching the
      original years_remaining_max semantics validated by the Task 1/2 fixture.
    - Any other condition key is treated as an exact-match constraint, checked only
      if the same key appears in **params.

    A rule with conditions the caller didn't supply any relevant params for will
    therefore still match -- narrowing happens only on dimensions the caller
    actually asked about.
    """
    for key, required_value in conditions.items():
        if key.endswith("_min"):
            base = key[: -len("_min")]
            if base in params and params[base] < required_value:
                return False
        elif key.endswith("_max"):
            base = key[: -len("_max")]
            if base in params and params[base] >= required_value:
                return False
        elif key in params and params[key] != required_value:
            return False
    return True


def _same_tier_conflicts(session: Session, rule: ProductionRule) -> list[ProductionRule]:
    """Same lookup semantics review_candidates.find_conflict() uses at promotion time."""
    others = session.scalars(
        select(ProductionRule).where(
            ProductionRule.lender == rule.lender,
            ProductionRule.debt_type == rule.debt_type,
            ProductionRule.source_tier == rule.source_tier,
            ProductionRule.id != rule.id,
        )
    ).all()
    return [o for o in others if o.conditions == rule.conditions and o.effect != rule.effect]


def _lookup_rates(session: Session, lender: str, as_of: date) -> list[RateMatch]:
    rates = session.scalars(select(Rate).where(Rate.lender == lender, Rate.effective_from <= as_of)).all()
    current = [r for r in rates if r.effective_to is None or r.effective_to >= as_of]
    return [
        RateMatch(
            lender=r.lender,
            product_name=r.product_name,
            rate_type=r.rate_type,
            rate_pct=r.rate_pct,
            source=_citation(r.source),
        )
        for r in current
    ]


def query_rule(
    session: Session,
    *,
    lender: str,
    debt_type: str,
    as_of: date | None = None,
    **params,
) -> RuleQueryResult:
    """
    The read-only query interface for this database (CONTEXT.md section 7): given a
    lender, a debt type, and whatever situational parameters are relevant (e.g.
    years_remaining=3 for the CBA HECS case), return the applicable production rule
    together with its source citation and confidence tier -- or a clean "no data"
    result if nothing matches.

    Guarantees:
    - Reads ONLY `production_rules` (never `candidate_rules` -- those are unreviewed
      and must never be surfaced as if they were authoritative, per section 6).
    - Makes no network call and calls no LLM -- every answer comes from rows already
      committed to the database; same inputs always produce the same output.
    - Never guesses: if nothing matches, `found` is False with an explanatory
      `message`, not an exception and not a best-effort default.
    - Never silently resolves a same-source_tier disagreement (CONTEXT.md 4c/4f): if
      more than one still-effective, condition-matching rule exists at the highest
      authority tier present with different effects, `conflicting_sources` is True
      and every disagreeing rule is returned in `conflicts` for a human/caller to
      resolve -- `match` is populated (to the first such rule) only for convenience
      and must not be trusted on its own when `conflicting_sources` is True.
    - Also returns any still-effective `rates` for the same lender, since a caller
      asking about a lender's policy will often want its current rates too -- rates
      are informational and don't affect `found`/`conflicting_sources`.
    - Never presents an unverified Layer 3 lead the same way as a corroborated or
      official_document-tier rule (CONTEXT.md 9c): every RuleMatch carries its own
      `verification_status`, and `confidence_warning` is set (non-None) whenever that
      status is "needs_corroboration". This does NOT suppress the result -- `match`
      is still populated -- it surfaces it as an unverified insight rather than a
      settled fact, per 9c's own wording.

    `as_of` supports point-in-time queries (CONTEXT.md 4e); defaults to today.
    Any extra keyword arguments (e.g. years_remaining=3) are matched against each
    candidate rule's `conditions` -- see _conditions_satisfied() for exact semantics.
    """
    as_of = as_of or date.today()

    candidates = session.scalars(
        select(ProductionRule).where(
            ProductionRule.lender == lender,
            ProductionRule.debt_type == debt_type,
            ProductionRule.effective_from <= as_of,
        )
    ).all()
    candidates = [
        r
        for r in candidates
        if (r.effective_to is None or r.effective_to >= as_of) and _conditions_satisfied(r.conditions, params)
    ]

    rates = _lookup_rates(session, lender, as_of)

    if not candidates:
        return RuleQueryResult(
            found=False,
            lender=lender,
            debt_type=debt_type,
            rates=rates,
            message=(
                f"No production rule found for lender={lender!r}, debt_type={debt_type!r} "
                f"matching parameters={params!r} as of {as_of.isoformat()}."
            ),
        )

    top_tier = min((r.source_tier for r in candidates), key=lambda t: _SOURCE_TIER_RANK[t])
    top_tier_candidates = [r for r in candidates if r.source_tier == top_tier]

    primary = top_tier_candidates[0]
    conflict_partners = {r.id: r for r in top_tier_candidates[1:] if r.effect != primary.effect}
    for extra in _same_tier_conflicts(session, primary):
        conflict_partners[extra.id] = extra

    if conflict_partners or primary.conflicting_sources:
        all_conflicting = [primary, *conflict_partners.values()]
        matches = [_to_rule_match(r) for r in all_conflicting]
        return RuleQueryResult(
            found=True,
            lender=lender,
            debt_type=debt_type,
            match=matches[0],
            conflicting_sources=True,
            conflicts=matches,
            rates=rates,
            message=(
                f"{len(matches)} conflicting production rules found at source_tier={top_tier!r} for "
                f"lender={lender!r}, debt_type={debt_type!r} -- resolve manually, do not treat "
                f"`match` alone as authoritative."
            ),
        )

    return RuleQueryResult(
        found=True,
        lender=lender,
        debt_type=debt_type,
        match=_to_rule_match(primary),
        rates=rates,
        message="OK",
    )
