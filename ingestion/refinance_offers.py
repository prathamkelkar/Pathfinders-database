"""
Refinance-offer ingestion -- fast-cadence promotional data (CONTEXT.md 4b's
rates/rules split, applied to cashback offers).

Reuses the existing Phase 2 change-detection machinery rather than duplicating
it: `get_latest_source()` and `CheckResult` come straight from
ingestion/change_detection.py, and the fetch/hash/save primitives from
ingestion/scrapers/base.py -- same arrangement ingestion/scrapers/
legislation_frl.py already uses for its own type-specific checker. The only
thing that differs here is what happens when a page HAS changed.

CADENCE (see ingestion/scheduled_pipeline.py): weekly, i.e. the fast lane
alongside rates rather than the monthly cadence used for lender T&Cs. Offers
don't drift continuously the way rates do, but they get launched and withdrawn
on a scale of weeks and have hard expiry cliffs, and stale offer data is
actively harmful once pillar 4 exists -- routing someone to a cashback that
ended last week is worse than saying nothing. Daily would be needless noise on
what are ultimately marketing pages.

WHY THIS DOESN'T AUTO-SUPERSEDE ON A PAGE CHANGE: a marketing page's hash
changes for all sorts of irrelevant reasons (a footer tweak, a rotating banner).
Treating any hash change as "the offer ended" would write false effective_to
dates into the versioned history -- corrupting exactly the point-in-time record
4e exists to protect. So a detected change persists a new `sources` row and
flags that the offer needs re-recording; deciding whether the OFFER actually
changed is a separate, deliberate step (record_refinance_offer below).
"""

import logging
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import RefinanceOffer, Source
from ingestion.change_detection import CheckResult, get_latest_source
from ingestion.scrapers.base import compute_content_hash, extract_text, fetch, save_raw_file

logger = logging.getLogger("ingestion.change_detection")

DEST_DIR = Path("sources/refinance_offers")


def check_refinance_offer_source(
    session: Session,
    *,
    lender: str,
    url: str,
    parent_entity: str | None = None,
    dest_dir: Path | None = None,
) -> CheckResult:
    """
    Fetch a lender's refinance/cashback page, hash it, and compare against the
    most recent stored source for that exact (lender, url) -- skipping entirely
    when nothing changed, exactly like check_lender_source() does for T&Cs.

    On new/changed content this persists a `sources` row and logs that the offer
    needs re-recording; it deliberately does NOT touch existing refinance_offers
    rows (see module docstring). `CheckResult`'s candidates_created /
    extraction_status fields are unused here -- offers don't go through the
    LLM extraction + human review flow, same as rates don't -- but the shared
    type keeps scheduled_pipeline's summary reporting uniform across check types.
    """
    dest_dir = dest_dir or DEST_DIR

    try:
        content = fetch(url)
    except Exception as e:
        logger.error("ERROR         entity=%s url=%s -- %s: %s", lender, url, type(e).__name__, e)
        return CheckResult(lender, url, "error", error=str(e))

    content_hash = compute_content_hash(content)
    previous = get_latest_source(session, lender, url)

    if previous is not None and previous.content_hash == content_hash:
        logger.info(
            "UNCHANGED     entity=%s url=%s (offer page matches source id=%s from %s)",
            lender, url, previous.id, previous.retrieved_date,
        )
        return CheckResult(lender, url, "unchanged", source=previous)

    filename = Path(urlparse(url).path).name or f"{content_hash[:16]}.html"
    dest_path = Path(dest_dir) / filename
    save_raw_file(content, dest_path)

    source = Source(
        entity_name=lender,
        parent_entity=parent_entity,
        source_tier="lender_official",
        url=url,
        retrieved_date=date.today(),
        raw_file_path=str(dest_path),
        raw_content=extract_text(content, url),
        content_hash=content_hash,
    )
    session.add(source)
    session.flush()

    status = "new" if previous is None else "changed"
    logger.info(
        "%-13s entity=%s url=%s new source id=%s -- refinance offer page changed, "
        "offer details need re-recording (record_refinance_offer)",
        status.upper(), lender, url, source.id,
    )
    session.commit()
    return CheckResult(lender, url, status, source=source)


def get_open_offer(session: Session, lender: str, offer_name: str | None) -> RefinanceOffer | None:
    """The currently-open (effective_to IS NULL) offer row for this lender+offer_name, if any."""
    return session.scalars(
        select(RefinanceOffer)
        .where(
            RefinanceOffer.lender == lender,
            RefinanceOffer.offer_name == offer_name,
            RefinanceOffer.effective_to.is_(None),
        )
        .order_by(RefinanceOffer.effective_from.desc(), RefinanceOffer.id.desc())
    ).first()


def record_refinance_offer(
    session: Session,
    *,
    source: Source,
    lender: str,
    offer_name: str | None = None,
    cashback_amount_aud: float | None = None,
    minimum_loan_amount_aud: float | None = None,
    maximum_lvr_pct: float | None = None,
    eligibility_conditions: dict | None = None,
    clawback_period_months: int | None = None,
    clawback_conditions: dict | None = None,
    other_benefits: dict | None = None,
    offer_expiry_date: date | None = None,
    parent_entity: str | None = None,
    observed_on: date | None = None,
) -> RefinanceOffer:
    """
    Record the current state of a lender's refinance offer, superseding (not
    overwriting) whatever was previously on record for that lender+offer_name.

    Versioning per 4e: the previous open row gets effective_to set to the day
    before this observation, so "what was <lender> advertising on <date>" stays
    answerable. If the new values are identical to the open row's, nothing is
    written and the existing row is returned unchanged -- re-recording an
    unchanged offer shouldn't fragment its history into daily duplicates.
    """
    observed_on = observed_on or date.today()
    eligibility_conditions = eligibility_conditions or {}
    clawback_conditions = clawback_conditions or {}
    other_benefits = other_benefits or {}

    existing = get_open_offer(session, lender, offer_name)
    if existing is not None:
        unchanged = (
            existing.cashback_amount_aud == cashback_amount_aud
            and existing.minimum_loan_amount_aud == minimum_loan_amount_aud
            and existing.maximum_lvr_pct == maximum_lvr_pct
            and existing.eligibility_conditions == eligibility_conditions
            and existing.clawback_period_months == clawback_period_months
            and existing.clawback_conditions == clawback_conditions
            and existing.other_benefits == other_benefits
            and existing.offer_expiry_date == offer_expiry_date
        )
        if unchanged:
            logger.info(
                "OFFER SAME    entity=%s offer=%r unchanged since %s -- no new version written",
                lender, offer_name, existing.effective_from,
            )
            return existing
        existing.effective_to = observed_on

    offer = RefinanceOffer(
        source_id=source.id,
        lender=lender,
        parent_entity=parent_entity,
        offer_name=offer_name,
        cashback_amount_aud=cashback_amount_aud,
        minimum_loan_amount_aud=minimum_loan_amount_aud,
        maximum_lvr_pct=maximum_lvr_pct,
        eligibility_conditions=eligibility_conditions,
        clawback_period_months=clawback_period_months,
        clawback_conditions=clawback_conditions,
        other_benefits=other_benefits,
        offer_expiry_date=offer_expiry_date,
        effective_from=observed_on,
        effective_to=None,
    )
    session.add(offer)
    session.flush()
    session.commit()

    logger.info(
        "OFFER RECORDED entity=%s offer=%r cashback=%s expiry=%s (id=%s%s)",
        lender, offer_name, cashback_amount_aud, offer_expiry_date, offer.id,
        f", superseded id={existing.id}" if existing is not None else "",
    )
    return offer
