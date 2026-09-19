"""
Recording explicit determinations about whether a policy_area applies to a
lender at all -- see db.models.PolicyAreaCoverage for why this needs its own
table rather than being inferable from missing rules.

The distinction this enforces, which the project got wrong before it existed:
"we found nothing" is not the same claim as "there is nothing to find". Three
lenders were reported as break-cost/discharge gaps needing new scraping when
the truth was different in every case -- Afterpay has no loan to discharge,
while MoneyMe and Wisr both publish an explicit early-repayment-fee policy
(it's nil) that we simply hadn't looked for in the right document.
"""

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import COVERAGE_STATUSES, POLICY_AREAS, PolicyAreaCoverage, Source

logger = logging.getLogger("ingestion.policy_coverage")


def record_coverage(
    session: Session,
    *,
    lender: str,
    policy_area: str,
    status: str,
    rationale: str,
    source: Source | None = None,
    assessed_on: date | None = None,
) -> PolicyAreaCoverage:
    """
    Record (or update) the determination for one lender + policy_area.

    Re-assessing overwrites the previous verdict rather than versioning it: unlike
    a rule, this isn't a fact about the world at a point in time, it's our current
    assessment of it, and keeping stale assessments around would just reintroduce
    the ambiguity the table exists to remove.
    """
    if policy_area not in POLICY_AREAS:
        raise ValueError(f"policy_area must be one of {POLICY_AREAS}, got {policy_area!r}")
    if status not in COVERAGE_STATUSES:
        raise ValueError(f"status must be one of {COVERAGE_STATUSES}, got {status!r}")
    if not rationale or not rationale.strip():
        raise ValueError(
            "a rationale is required -- an unexplained 'not_applicable' is exactly the "
            "unfalsifiable claim this table exists to prevent"
        )

    existing = session.scalars(
        select(PolicyAreaCoverage).where(
            PolicyAreaCoverage.lender == lender,
            PolicyAreaCoverage.policy_area == policy_area,
        )
    ).first()

    if existing is not None:
        existing.status = status
        existing.rationale = rationale
        existing.source_id = source.id if source is not None else None
        existing.assessed_on = assessed_on or date.today()
        session.commit()
        logger.info("COVERAGE UPDATED %s/%s -> %s", lender, policy_area, status)
        return existing

    coverage = PolicyAreaCoverage(
        lender=lender,
        policy_area=policy_area,
        status=status,
        rationale=rationale,
        source_id=source.id if source is not None else None,
        assessed_on=assessed_on or date.today(),
    )
    session.add(coverage)
    session.commit()
    logger.info("COVERAGE RECORDED %s/%s -> %s", lender, policy_area, status)
    return coverage


def get_coverage(session: Session, *, lender: str, policy_area: str) -> PolicyAreaCoverage | None:
    """None means NEVER ASSESSED -- deliberately distinct from a recorded not_applicable."""
    return session.scalars(
        select(PolicyAreaCoverage).where(
            PolicyAreaCoverage.lender == lender,
            PolicyAreaCoverage.policy_area == policy_area,
        )
    ).first()
