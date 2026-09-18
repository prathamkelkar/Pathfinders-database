"""
Change-detection layer sitting on top of the existing per-source fetch logic
(ingestion/scrapers/base.py). Re-running a scraper unconditionally would
re-insert an identical `sources` row and re-run (expensive, slow) LLM
extraction every single time, even when a lender hasn't touched their T&Cs in
months. Instead: fetch, hash, compare against the most recent stored `sources`
row for that exact (entity_name, url), and only persist + extract when the
hash actually differs.

This module provides the generic, content-agnostic version used by lender
scrapers (which store extracted text as-is). Legislation has its own
check_legislation_targets() in ingestion/scrapers/legislation_frl.py, because
it needs to prepend the compilation-metadata annotation block before
comparing/storing content -- see that module's docstring for why legislation
isn't forced through this same generic path.

Nothing here ever writes to production_rules -- extraction still only
produces candidate_rules, same as every other extraction path in this
project. A human still has to run ingestion/review_candidates.py.
"""

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Source
from ingestion.extraction.extract_rules import ExtractionFailedError, extract_candidates
from ingestion.scrapers.base import compute_content_hash, extract_text, fetch, save_raw_file

logger = logging.getLogger("ingestion.change_detection")


@dataclass
class CheckResult:
    entity_name: str
    url: str
    status: str  # "unchanged" | "new" | "changed" | "error"
    source: Source | None = None
    candidates_created: int = 0
    error: str | None = None
    # "ok" (>=0 candidates from a clean parse) | "extraction_failed_after_retries"
    # (model never returned usable JSON) | None (extraction wasn't run). Kept
    # distinct from candidates_created==0 so a genuinely-empty document isn't
    # confused with the model failing to respond usably.
    extraction_status: str | None = None


def get_latest_source(session: Session, entity_name: str, url: str) -> Source | None:
    """Most recent stored source for this exact (entity_name, url) pair, if any."""
    return session.scalars(
        select(Source)
        .where(Source.entity_name == entity_name, Source.url == url)
        .order_by(Source.retrieved_date.desc(), Source.id.desc())
    ).first()


def check_lender_source(
    session: Session,
    *,
    entity_name: str,
    parent_entity: str | None,
    url: str,
    dest_dir: Path,
    source_tier: str = "lender_official",
    run_extraction: bool = True,
) -> CheckResult:
    """
    Fetch `url`, compare its hash to the most recently stored source for this
    (entity_name, url), and only persist a new `sources` row + run extraction
    if it's new or actually changed. Always logs the outcome, including the
    unchanged case, so a scheduled run leaves visible evidence it executed
    even when nothing needed to happen.
    """
    try:
        content = fetch(url)
    except Exception as e:
        logger.error("ERROR         entity=%s url=%s -- %s: %s", entity_name, url, type(e).__name__, e)
        return CheckResult(entity_name, url, "error", error=str(e))

    content_hash = compute_content_hash(content)
    previous = get_latest_source(session, entity_name, url)

    if previous is not None and previous.content_hash == content_hash:
        logger.info(
            "UNCHANGED     entity=%s url=%s (matches source id=%s from %s)",
            entity_name, url, previous.id, previous.retrieved_date,
        )
        return CheckResult(entity_name, url, "unchanged", source=previous)

    filename = Path(urlparse(url).path).name or f"{content_hash[:16]}.bin"
    dest_path = Path(dest_dir) / filename
    save_raw_file(content, dest_path)

    source = Source(
        entity_name=entity_name,
        parent_entity=parent_entity,
        source_tier=source_tier,
        url=url,
        retrieved_date=date.today(),
        raw_file_path=str(dest_path),
        raw_content=extract_text(content, url),
        content_hash=content_hash,
    )
    session.add(source)
    session.flush()

    status = "new" if previous is None else "changed"
    logger.info("%-13s entity=%s url=%s new source id=%s", status.upper(), entity_name, url, source.id)

    candidates_created = 0
    extraction_status = None
    if run_extraction:
        try:
            candidates = extract_candidates(session, source)
            candidates_created = len(candidates)
            extraction_status = "ok"
            logger.info(
                "EXTRACTED     entity=%s source id=%s -> %d candidate(s) ready for review",
                entity_name, source.id, candidates_created,
            )
        except ExtractionFailedError as e:
            extraction_status = "extraction_failed_after_retries"
            logger.error(
                "EXTRACTION_FAILED_AFTER_RETRIES  entity=%s source id=%s -- %s",
                entity_name, source.id, e,
            )
        except Exception as e:
            extraction_status = "extraction_error"
            logger.error(
                "EXTRACTION FAILED  entity=%s source id=%s -- %s: %s",
                entity_name, source.id, type(e).__name__, e,
            )

    session.commit()
    return CheckResult(
        entity_name, url, status, source=source,
        candidates_created=candidates_created, extraction_status=extraction_status,
    )
