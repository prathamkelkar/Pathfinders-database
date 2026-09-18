"""
SQLAlchemy models for the Australian Debt Policy Database.

Layer summary (see CONTEXT.md section 4):
- Source: raw document provenance. Everything else links back here (4g).
- RuleFieldsMixin: shared columns for candidate/production rules (4c, 4d, 4e, 4f, 4h, 4i).
- CandidateRule / ProductionRule: same shape, physically separate tables (section 6) —
  extraction writes to CandidateRule, a human promotes rows into ProductionRule.
- CorroboratingSource: Layer 3 corroboration gate for broker_sourced production
  rules (9c) -- see ingestion/layer3_verification.py.
- Rate: decoupled from rules, its own refresh cadence (4b).
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

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    candidate_rules: Mapped[list["CandidateRule"]] = relationship(back_populates="source")
    production_rules: Mapped[list["ProductionRule"]] = relationship(back_populates="source")
    rates: Mapped[list["Rate"]] = relationship(back_populates="source")


class RuleFieldsMixin:
    """
    Shared columns for CandidateRule and ProductionRule.

    `conditions` and `effect` are JSON blobs rather than typed columns because rule shapes
    vary wildly across debt types (HECS buffers vs. redraw terms vs. family law offsetting) —
    see CONTEXT.md section 2. The columns that must be queryable/joinable across all rule
    types (lender, debt_type, tiers, dates) stay as real typed columns.

    Example for the CBA HECS case (CONTEXT.md section 2):
        lender = "CBA", debt_type = "HECS_HELP"
        conditions = {"years_remaining_max": 1}
        effect = {"treatment": "excluded_from_serviceability"}
    and:
        conditions = {"years_remaining_min": 1, "years_remaining_max": 5}
        effect = {"treatment": "reduced_buffer", "buffer_pct": 1.0, "default_buffer_pct": 3.0}
    """

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # --- Identity / applicability ---
    lender: Mapped[str] = mapped_column(String, nullable=False)
    debt_type: Mapped[str] = mapped_column(String, nullable=False)
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
