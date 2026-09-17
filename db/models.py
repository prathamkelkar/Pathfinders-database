"""
SQLAlchemy models for the Australian Debt Policy Database.

Layer summary (see CONTEXT.md section 4):
- Source: raw document provenance. Everything else links back here (4g).
- RuleFieldsMixin: shared columns for candidate/production rules (4c, 4d, 4e, 4f, 4h, 4i).
- CandidateRule / ProductionRule: same shape, physically separate tables (section 6) —
  extraction writes to CandidateRule, a human promotes rows into ProductionRule.
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

    url: Mapped[str] = mapped_column(String, nullable=False)
    retrieved_date: Mapped[date] = mapped_column(Date, nullable=False)

    # Pointer to the cached raw file under /sources/, not the content itself (4a).
    raw_file_path: Mapped[str | None] = mapped_column(String, nullable=True)

    # Optional inline copy of raw text for small/simple sources (e.g. a short webpage).
    raw_content: Mapped[str | None] = mapped_column(Text, nullable=True)

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
