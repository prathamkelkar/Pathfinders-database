"""
SQLAlchemy models for the Australian Debt Policy Database.

Layer summary (see CONTEXT.md section 4):
- Source: raw document provenance. Everything else links back here (4g).
- RuleFieldsMixin: shared columns for candidate/production rules (4c, 4d, 4e, 4f, 4h, 4i).
- CandidateRule / ProductionRule: same shape, physically separate tables (section 6) —
  extraction writes to CandidateRule, a human promotes rows into ProductionRule.
- CorroboratingSource: Layer 3 corroboration gate for broker_sourced production
  rules (9c) -- see ingestion/layer3_verification.py.
- Rate / RefinanceOffer: fast-cadence commodity + promotional data, decoupled from
  rules and from each other (4b) -- see RefinanceOffer's docstring for why these
  are two tables rather than one.
"""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

SOURCE_TIERS = (
    "statute",
    "regulation",
    "regulator_guidance",
    "lender_official",
    "broker_sourced",
)

CONFIDENCE_TIERS = (
    "official_document",
    "corroborated_broker_source",
    "single_anecdotal_source",
)

LEGISLATION_STATUSES = (
    "proposed",
    "introduced_to_parliament",
    "enacted",
    "in_force",
)

# CONTEXT.md 9c: Layer 3 (broker_sourced) rules start here and can only move to
# "corroborated" (2nd independent source linked) or "directly_confirmed" (a broker
# confirmed the specific candidate) via ingestion/layer3_verification.py -- never
# by being set directly at insert/promotion time.
VERIFICATION_STATUSES = (
    "needs_corroboration",
    "corroborated",
    "directly_confirmed",
)

# CONTEXT.md 9b's yield-ranked categories of Layer 3 evidence, used to judge whether
# two corroborating sources for the same rule are actually independent of each other.
LAYER3_SOURCE_TYPES = (
    "regulator_guidance",
    "broker_content_marketing",
    "trade_press",
    "broker_social",
    "comparison_site",
    "calculator_probing",
    "broker_interview",
)

# Whether a policy_area applies to a lender at all, and if so whether we hold it.
# Absence of a PolicyAreaCoverage row means "never assessed" -- that's the null
# case, deliberately NOT an enum value, so it can never be confused with a positive
# finding of non-applicability.
COVERAGE_STATUSES = (
    "not_applicable",        # assessed: this lender's products have no such concept
    "applicable_not_found",  # applies, but we don't hold the policy yet
    "applicable_found",      # applies, and we hold at least one rule for it
)

# Which ASPECT of a lender's handling of a debt_type this rule is about -- orthogonal
# to debt_type itself (e.g. multiple policy_area rules can share debt_type="home_loan").
# "serviceability" covers the project's original scope (how debt affects borrowing
# assessment); the other three were added to support refinancing/switching decisions
# per CONTEXT.md section 3's revised category coverage. Nullable on rules: rows
# extracted before this column existed predate the dimension entirely (not
# miscategorized -- simply not yet classified under it) and are left None rather
# than backfilled with a guess.
POLICY_AREAS = (
    "serviceability",     # how this debt is treated in a borrowing-capacity assessment
    "break_cost",         # early exit / break fees for ending a loan or fixed term early
    "discharge",          # process and timeline to fully discharge and close out a loan
    "switching",          # internal product/rate-type switching, no full refinance
)


class Base(DeclarativeBase):
    pass


class Source(Base):
    """Raw document provenance. Every rule and rate must trace back to one of these (4g)."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Who/what this source is about — e.g. "CBA", "legislation.gov.au", "ATO".
    entity_name: Mapped[str] = mapped_column(String, nullable=False)

    # Explicit entity resolution for white-labels/subsidiaries sharing upstream policy (4h).
    parent_entity: Mapped[str | None] = mapped_column(String, nullable=True)

    source_tier: Mapped[str] = mapped_column(Enum(*SOURCE_TIERS, name="source_tier"), nullable=False)

    # Nullable: Layer 3 (broker-sourced) rules have no public document to link to --
    # that's the defining feature of that layer (CONTEXT.md section 2). For those,
    # raw_content holds the human-written provenance note instead.
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    retrieved_date: Mapped[date] = mapped_column(Date, nullable=False)

    # Pointer to the cached raw file under /sources/, not the content itself (4a).
    raw_file_path: Mapped[str | None] = mapped_column(String, nullable=True)

    # Optional inline copy of raw text for small/simple sources (e.g. a short webpage),
    # or extracted text from raw_file_path for larger documents (e.g. a PDF).
    raw_content: Mapped[str | None] = mapped_column(Text, nullable=True)

    # sha256 of the raw file bytes -- lets a scheduled re-scrape detect whether the
    # underlying document actually changed before re-extracting (4a).
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)

    # Distribution restriction stated ON the document, where one exists. Several
    # lenders serve broker-facing credit policy from public, unauthenticated URLs
    # while stamping it "For broker purposes only" or "CONFIDENTIAL". Robots.txt
    # permits fetching and the content is the best serviceability evidence
    # available, but the restriction is a fact about the source and belongs with
    # its provenance (4g) rather than being silently dropped -- anyone later
    # deciding whether to quote or redistribute a rule needs to see it.
    access_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    candidate_rules: Mapped[list["CandidateRule"]] = relationship(back_populates="source")
    production_rules: Mapped[list["ProductionRule"]] = relationship(back_populates="source")
    rates: Mapped[list["Rate"]] = relationship(back_populates="source")
    refinance_offers: Mapped[list["RefinanceOffer"]] = relationship(back_populates="source")
    policy_area_coverage: Mapped[list["PolicyAreaCoverage"]] = relationship(back_populates="source")


class RuleFieldsMixin:
    """
    Shared columns for CandidateRule and ProductionRule.

    `conditions` and `effect` are JSON blobs rather than typed columns because rule shapes
    vary wildly across debt types (HECS buffers vs. redraw terms vs. family law offsetting) —
    see CONTEXT.md section 2. The columns that must be queryable/joinable across all rule
    types (lender, debt_type, tiers, dates) stay as real typed columns.

    Example for the CBA HECS case (CONTEXT.md section 2), policy_area="serviceability":
        lender = "CBA", debt_type = "HECS_HELP"
        conditions = {"years_remaining_max": 1}
        effect = {"treatment": "excluded_from_serviceability"}
    and:
        conditions = {"years_remaining_min": 1, "years_remaining_max": 5}
        effect = {"treatment": "reduced_buffer", "buffer_pct": 1.0, "default_buffer_pct": 3.0}

    Conventions for the other policy_area values added for refinancing/switching
    coverage (CONTEXT.md section 3) -- same reasoning as the HECS case: shapes vary
    per rule, so these are documented conventions for `conditions`/`effect`, not
    separate typed columns:

    policy_area="break_cost" -- the whole point is that break costs are usually a
    FORMULA, not a flat number, so effect always carries a description even when a
    flat fee also applies. Real example, CBA (candidate_rule id=4, from its own
    T&Cs): conditions = {"loan_type": "Fixed Rate", "action": "prepayment exceeding
    $10,000 per fixed term year or full payoff earlier than expected"}, effect =
    {"early_repayment_adjustment_charged": true, "administrative_fee_charged": true,
    "calculation_basis": "difference between wholesale market swap rate at fixing
    and at prepayment for balance of fixed period", "maximum": "estimate of Bank's
    loss"}. `calculation_basis` (or an equivalent descriptive key) holds the
    formula/method in plain English -- there's no `fee_amount_aud`-only path for a
    genuine break-cost rule, because that would misrepresent a variable, formula-
    driven cost as a fixed one.

    policy_area="discharge" -- e.g. conditions = {"loan_type": "any"}, effect =
    {"process_description": "borrower submits a discharge authority form; bank
    prepares and lodges the discharge with the land titles office", "typical_timeline_days":
    10, "fee_aud": 350}.

    policy_area="switching" -- e.g. conditions = {"from_product": "variable_rate",
    "to_product": "fixed_rate"}, effect = {"requires_full_refinance": false,
    "fee_aud": 0, "process_description": "internal rate-type switch via online
    banking or a switch form, no new credit assessment required"}.
    """

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # --- Identity / applicability ---
    lender: Mapped[str] = mapped_column(String, nullable=False)
    debt_type: Mapped[str] = mapped_column(String, nullable=False)

    # Which aspect of debt_type this rule covers (see POLICY_AREAS above) -- a real
    # column, not JSON, specifically so query_rule() can filter by it directly rather
    # than relying on `conditions` alone. Without this, a break_cost rule and a
    # serviceability rule that both happen to share debt_type="home_loan" and don't
    # constrain some of the same condition keys can spuriously "both match" a query
    # that wasn't asking about either dimension -- the same ambiguity already
    # observed between NAB's self-employed and rental-shading rules. Nullable: see
    # POLICY_AREAS' comment on pre-existing rows.
    policy_area: Mapped[str | None] = mapped_column(Enum(*POLICY_AREAS, name="policy_area"), nullable=True)

    conditions: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    effect: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Human-readable plain-English restatement of the rule. Mainly useful for
    # manually-authored Layer 3 entries, where conditions/effect alone may not be
    # self-explanatory to a future reader the way an LLM-extracted clause is.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Source authority (4c) ---
    source_tier: Mapped[str] = mapped_column(Enum(*SOURCE_TIERS, name="rule_source_tier"), nullable=False)

    # --- Legislation lifecycle, nullable for lender-sourced rules (4d) ---
    legislation_status: Mapped[str | None] = mapped_column(
        Enum(*LEGISLATION_STATUSES, name="legislation_status"), nullable=True
    )
    enacted_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    commencement_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # --- Versioning via effective-date ranges, never overwritten (4e) ---
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    # --- Confidence and conflict flags (4f) ---
    confidence: Mapped[str] = mapped_column(Enum(*CONFIDENCE_TIERS, name="confidence_tier"), nullable=False)
    conflicting_sources: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    conflict_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Extraction versioning (4i) ---
    extraction_prompt_version: Mapped[str | None] = mapped_column(String, nullable=True)

    # --- Layer 3 corroboration gate (9c) -- nullable because this mainly applies to
    # source_tier="broker_sourced" rules (statute/regulation/lender_official rules
    # normally have their own document as sufficient provenance and skip this gate).
    # Exception: calculator-probe candidates (9b.5) are source_tier="lender_official"
    # -- it's the lender's own tool -- but the INFERRED rule behind the numbers is
    # still just as speculative as a broker tip, so they go through this gate too
    # (see ingestion/calculator_probe_intake.py).
    verification_status: Mapped[str | None] = mapped_column(
        Enum(*VERIFICATION_STATUSES, name="verification_status"), nullable=True
    )

    # Which CONTEXT.md 9b evidence category this candidate came from, when it isn't
    # a plain scraped/extracted document (e.g. "calculator_probing", "broker_social").
    # Nullable -- ordinary Layer 1/2 extraction leaves this unset.
    source_type: Mapped[str | None] = mapped_column(
        Enum(*LAYER3_SOURCE_TYPES, name="candidate_source_type"), nullable=True
    )

    # --- Arithmetic plausibility check (9d) -- a SUPPORTING SIGNAL ONLY, never a
    # gate. Stores ingestion.layer3_plausibility.PlausibilityCheckResult.to_dict()
    # when a claimed numeric scenario (e.g. the CBA $490k->$600k example) has been
    # checked, for a human reviewer to see. Null when no scenario was ever checked
    # -- absence means "not assessed", not "assessed as fine".
    plausibility_check: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class CandidateRule(RuleFieldsMixin, Base):
    """Extraction output, awaiting human review (section 6). Not queried at runtime."""

    __tablename__ = "candidate_rules"

    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    source: Mapped["Source"] = relationship(back_populates="candidate_rules")

    # Set by the review script when a candidate is promoted.
    promoted_to_production_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("production_rules.id"), nullable=True
    )

    # Reviewed and explicitly rejected. Without this, the review queue has only two
    # states -- promoted or not-yet-promoted -- so a candidate a human has already
    # looked at and dismissed (e.g. NCCP boilerplate extracted from a Credit Guide,
    # which says nothing about that lender's actual policy) resurfaces in every
    # future review pass forever. Rejected rows are kept, not deleted: the fact that
    # a bad extraction happened is itself useful signal about a prompt or a source.
    rejected_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    corroborating_sources: Mapped[list["CorroboratingSource"]] = relationship(
        back_populates="candidate_rule", cascade="all, delete-orphan"
    )


class ProductionRule(RuleFieldsMixin, Base):
    """Human-reviewed, authoritative rules. This is what the read-only query interface hits (4a, section 7)."""

    __tablename__ = "production_rules"

    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    source: Mapped["Source"] = relationship(back_populates="production_rules")

    # Which candidate this was promoted from, for auditability. Nullable to allow
    # manually-entered production rules (e.g. seeding from CONTEXT.md fixtures).
    promoted_from_candidate_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_rules.id"), nullable=True
    )

    corroborating_sources: Mapped[list["CorroboratingSource"]] = relationship(
        back_populates="production_rule", cascade="all, delete-orphan"
    )


class CorroboratingSource(Base):
    """
    One piece of evidence for a Layer 3 (broker_sourced, or lender_official via
    calculator-probing -- 9b.5) rule -- CONTEXT.md 9c. This includes the ORIGINAL
    lead that justified the rule in the first place, not just subsequent
    corroboration: verification_status is derived from the full list of entries
    here, via ingestion/layer3_verification.compute_verification_status().

    Links to EITHER a CandidateRule or a ProductionRule, never both -- most
    corroboration work happens pre-promotion (a human finds a second source
    before ever promoting the candidate), but a rule already promoted can still
    gain corroboration later too.
    """

    __tablename__ = "corroborating_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    candidate_rule_id: Mapped[int | None] = mapped_column(ForeignKey("candidate_rules.id"), nullable=True)
    candidate_rule: Mapped["CandidateRule"] = relationship(back_populates="corroborating_sources")

    production_rule_id: Mapped[int | None] = mapped_column(ForeignKey("production_rules.id"), nullable=True)
    production_rule: Mapped["ProductionRule"] = relationship(back_populates="corroborating_sources")

    source_type: Mapped[str] = mapped_column(Enum(*LAYER3_SOURCE_TYPES, name="layer3_source_type"), nullable=False)

    # Different sourcing channels carry different evidence -- a URL for a blog post
    # or comparison-site article, nothing for a verbal broker interview, etc.
    url_or_reference: Mapped[str | None] = mapped_column(String, nullable=True)

    # Distinguishes "different broker" corroboration (9c) from "same broker, posted
    # twice" -- null when the broker's identity genuinely isn't known/trackable.
    broker_identifier: Mapped[str | None] = mapped_column(String, nullable=True)

    # Date of the evidence itself (when the broker posted/said it), not when we found
    # it -- this is what 9d's recency check re-verifies against.
    date: Mapped[date] = mapped_column(Date, nullable=False)

    # True only when a broker has directly confirmed THIS SPECIFIC candidate rule
    # when asked -- the strongest tier (9c), overrides plain corroboration.
    is_direct_confirmation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class PolicyAreaCoverage(Base):
    """
    Records an explicit determination about whether a given policy_area even
    APPLIES to a given lender -- and if it does, whether we've found it yet.

    Why this can't live on production_rules: the whole point is to record the
    ABSENCE of a rule, and you cannot represent "there is no such rule, and
    there never will be" as a rule row. Without this table, four genuinely
    different situations all look identical (no rows returned):

      - never assessed                     -> no row here at all
      - assessed, genuinely doesn't apply  -> status="not_applicable"
      - applies, we haven't found it yet   -> status="applicable_not_found"
      - applies, we hold rules for it      -> status="applicable_found"

    That ambiguity caused real waste: Afterpay, MoneyMe and Wisr were all
    reported as break-cost/discharge "gaps" needing new scraping, when in fact
    Afterpay is BNPL with no loan to discharge, and MoneyMe's own TMD already
    stated an explicit no-early-payout-penalty policy that we had already
    extracted. `rationale` exists so the reasoning is recorded with the verdict
    rather than living only in a chat log.
    """

    __tablename__ = "policy_area_coverage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    lender: Mapped[str] = mapped_column(String, nullable=False)
    policy_area: Mapped[str] = mapped_column(Enum(*POLICY_AREAS, name="coverage_policy_area"), nullable=False)
    status: Mapped[str] = mapped_column(Enum(*COVERAGE_STATUSES, name="coverage_status"), nullable=False)

    # Why this verdict was reached -- e.g. which products the lender actually
    # offers, or which document class was checked. Required: a bare
    # "not_applicable" with no reasoning is exactly the unfalsifiable claim this
    # table exists to prevent.
    rationale: Mapped[str] = mapped_column(Text, nullable=False)

    # Evidence for the determination where one exists (4g). Nullable because
    # "this lender offers no mortgage product" is established from their product
    # range rather than from a clause in a specific document.
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    source: Mapped["Source"] = relationship(back_populates="policy_area_coverage")

    assessed_on: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class RefinanceOffer(Base):
    """
    Refinance cashback/promotional offers -- CONTEXT.md section 3's revised category
    coverage ("refinance-specific offers (cashback, rate discounts) and their
    conditions").

    Deliberately its own table, separate from BOTH:
    - `rates`: different shape entirely. A cashback offer isn't an interest rate --
      it has eligibility gates, a clawback tail, and a hard expiry date that a rate
      doesn't have. Cramming it into `rates` would mean most columns null for most
      rows.
    - `production_rules`: different cadence and different nature. Per 4b's
      rates/rules split, this is fast-moving promotional data (offers get launched,
      changed and withdrawn on a scale of weeks), not structural policy that changes
      1-4x/year. It also carries no confidence/conflict/verification fields for the
      same reason `rates` doesn't: a published cashback figure is a quoted number,
      not inferred policy, so the Layer 3 corroboration machinery (9c) doesn't apply.

    What it DOES keep from the rules side: full source citation (4g -- every row
    traces to a `sources` record) and effective-date versioning (4e -- superseded
    offers are closed off with effective_to, never overwritten), because
    "what cashback was this lender advertising on date X" is a real point-in-time
    question once pillar 4 (execution/routing) exists.

    Note the two different dates, which are NOT the same thing (same distinction as
    4d's enacted vs commencement dates):
    - effective_from / effective_to: OUR record of when we observed this offer to be
      the current one.
    - offer_expiry_date: the LENDER's own stated expiry. An offer can be past its
      expiry date while still being our latest record of it (because nobody has
      re-scraped since) -- which is exactly the stale-data case worth detecting, and
      it's detectable with no network call at all. See
      db.query.get_active_refinance_offers().
    """

    __tablename__ = "refinance_offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    source: Mapped["Source"] = relationship(back_populates="refinance_offers")

    lender: Mapped[str] = mapped_column(String, nullable=False)
    # Entity resolution (4h) -- two brands of the same ADI often run the identical offer.
    parent_entity: Mapped[str | None] = mapped_column(String, nullable=True)

    offer_name: Mapped[str | None] = mapped_column(String, nullable=True)

    # Nullable: not every refinance offer is a cashback -- some are rate discounts or
    # fee waivers, captured in other_benefits instead.
    cashback_amount_aud: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- Eligibility gates. Real columns for the two that every offer states and
    # that callers will actually filter/compare on; JSON for the long tail
    # (owner-occupier only, P&I only, new-to-bank only, broker-channel only, ...).
    minimum_loan_amount_aud: Mapped[float | None] = mapped_column(Float, nullable=True)
    maximum_lvr_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    eligibility_conditions: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # --- Clawback: the part with real consumer consequences. The period is the
    # queryable/comparable bit; the rest (amount repayable, trigger events, whether
    # it's pro-rata) varies enough per lender to belong in JSON.
    clawback_period_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clawback_conditions: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    other_benefits: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # The lender's own stated expiry (see class docstring) -- null when the offer is
    # advertised as ongoing/until-withdrawn rather than with a fixed end date.
    offer_expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # --- Our own versioning (4e) ---
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Rate(Base):
    """
    Interest rates and similar fast-moving commodity data — decoupled from rules,
    refreshed far more often (4b). Deliberately has no confidence/conflict/legislation
    fields: rates are quoted numbers, not inferred policy.
    """

    __tablename__ = "rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    source: Mapped["Source"] = relationship(back_populates="rates")

    lender: Mapped[str] = mapped_column(String, nullable=False)
    product_name: Mapped[str] = mapped_column(String, nullable=False)
    rate_type: Mapped[str] = mapped_column(String, nullable=False)  # e.g. "variable", "fixed_3yr"
    rate_pct: Mapped[float] = mapped_column(Float, nullable=False)

    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
