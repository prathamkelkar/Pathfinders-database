"""
Federal Register of Legislation (legislation.gov.au) scraper.

Deliberately NOT a copy of the lender pattern (ingestion/scrapers/cba.py etc.),
because legislation has a genuinely different structure that those scrapers
don't need to handle:

- The Register publishes versioned "compilations" of an Act -- a compilation
  number, a compilation date (when that consolidated version took effect),
  and a list of amending Acts included. This is the legislative equivalent of
  CONTEXT.md 4d/4e's status + commencement-date + versioning requirements, and
  it's stated in plain text on page 1 of every downloaded volume.
- Large Acts are split into multiple numbered volumes by the Register itself
  (not by us) -- Family Law Act 1975, Corporations Act 2001, and the National
  Consumer Credit Protection Act 2009 are each several hundred pages, so each
  target below specifies exactly which volume contains the provision of
  interest, rather than fetching "the document" the way a lender PDF is one
  self-contained file.
- This site is a client-rendered single-page app: fetching /latest,
  /latest/details, or /latest/downloads with a plain HTTP GET returns only the
  Angular app shell, not real data (confirmed by inspecting raw response
  bytes) -- there is no "url that resolves to the newest version" the way
  there is for a lender's PDS page. Only the actual dated document URLs
  (.../<act_id>/<compilation_date>/<compilation_date>/text/original/pdf/<volume>)
  serve real content. That means these URLs are pinned to specific compilation
  dates and WILL need manual updating whenever a new compilation is
  registered -- see TARGETS below. This is a known limitation, not an
  oversight; there is no reliable "latest" endpoint to poll instead without
  reverse-engineering the Register's private API.

CONTENT LICENCE (checked at https://www.legislation.gov.au/terms-of-use):
Material on the Register is licensed under Creative Commons Attribution 4.0
International (CC BY 4.0) -- shareable and adaptable, provided attribution is
given. The Register's own required attribution wording (for unchanged content)
is: "Sourced from the Federal Register of Legislation at [date]. For the
latest information on Australian Government law please go to
https://www.legislation.gov.au." Two exceptions apply: material marked as
third-party copyright, and the Commonwealth Coat of Arms. Neither applies to
the plain statutory text scraped here. That attribution string is embedded
directly in every stored source's raw_content below.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from db.models import Source
from ingestion.scrapers.base import compute_content_hash, extract_text, fetch, save_raw_file

logger = logging.getLogger("ingestion.change_detection")

DEST_DIR = Path("sources/legislation")


@dataclass(frozen=True)
class LegislationTarget:
    act_name: str
    act_id: str  # FRL register ID for the Act itself, e.g. "C2004A00275"
    compilation_date: str  # YYYY-MM-DD of the specific compilation this URL serves (see module docstring)
    volume: int
    provision_of_interest: str  # human-readable note on what we actually want from this volume


# Verified 2026-09-18 by downloading each PDF directly and reading its own front
# matter (Compilation No./date, section range per volume) -- not taken from the
# source inventory's URLs at face value, since one of those (the Corporations
# Act entry) turned out to be wrong (see ingestion/source_inventory.md follow-up).
TARGETS = [
    LegislationTarget(
        act_name="Family Law Act 1975",
        act_id="C2004A00275",
        compilation_date="2025-06-10",
        volume=1,
        provision_of_interest="s4AA - definition of de facto relationship",
    ),
    LegislationTarget(
        act_name="Corporations Act 2001",
        act_id="C2004A00818",
        compilation_date="2026-08-27",
        volume=4,
        provision_of_interest="Chapter 7, Part 7.7 - financial product advice provisions (sections 760A-994Q)",
    ),
    LegislationTarget(
        act_name="National Consumer Credit Protection Act 2009",
        act_id="C2009A00134",
        compilation_date="2026-07-01",
        volume=1,
        provision_of_interest="Chapter 2, Part 2-2 - requirement for an Australian Credit Licence (sections 1-322)",
    ),
]

ATTRIBUTION_TEMPLATE = (
    "Sourced from the Federal Register of Legislation at {retrieved_date}. "
    "For the latest information on Australian Government law please go to "
    "https://www.legislation.gov.au."
)

# Matches the front-matter block every FRL compilation PDF starts with, e.g.:
#   "Compilation No. 148"
#   "Compilation date: 27 August 2026"
#   "Includes amendments: Act No. 69, 2026"
#   "Authorised Version C2026C00382 registered 09/09/2026"
_COMPILATION_NO_RE = re.compile(r"Compilation No\.?\s*(\d+)")
_COMPILATION_DATE_RE = re.compile(r"Compilation date:\s*([\d]{1,2} [A-Za-z]+ \d{4})")
_AMENDMENTS_RE = re.compile(r"Includes amendments:\s*(.+)")
_AUTHORISED_VERSION_RE = re.compile(r"Authorised Version (\S+) registered (\d{2}/\d{2}/\d{4})")


def _build_pdf_url(target: LegislationTarget) -> str:
    d = target.compilation_date
    return f"https://www.legislation.gov.au/{target.act_id}/{d}/{d}/text/original/pdf/{target.volume}"


def parse_compilation_metadata(front_matter_text: str) -> dict:
    """
    Deterministically parse the compilation front matter that FRL prints on
    page 1 of every volume -- this is what lets us set source_tier=statute with
    a real, code-verified commencement date rather than asking the LLM to
    guess it from unstructured text (CONTEXT.md 4d).
    """
    compilation_no = _COMPILATION_NO_RE.search(front_matter_text)
    compilation_date = _COMPILATION_DATE_RE.search(front_matter_text)
    amendments = _AMENDMENTS_RE.search(front_matter_text)
    authorised = _AUTHORISED_VERSION_RE.search(front_matter_text)

    return {
        "compilation_no": compilation_no.group(1) if compilation_no else None,
        "compilation_date": compilation_date.group(1) if compilation_date else None,
        "includes_amendments": amendments.group(1).strip() if amendments else None,
        "authorised_version_id": authorised.group(1) if authorised else None,
        "authorised_version_registered": authorised.group(2) if authorised else None,
    }


def build_metadata_block(target: LegislationTarget, metadata: dict, retrieved_date: date) -> str:
    """
    A deterministic, code-generated annotation prepended to raw_content. This is
    what makes the compilation/commencement/status information "explicit"
    (as opposed to left for the LLM to infer from wherever it appears in the
    raw PDF text) while requiring no schema change: extract_rules.py already
    looks for exactly this kind of stated status/date information when the
    source is legislation (see its SYSTEM_PROMPT), so this block gives it
    unambiguous, correctly-labelled facts to read rather than raw front matter
    it would have to interpret itself.

    Status is set to "in_force" because a Register-published current
    compilation of a principal Act represents the law as currently in force by
    definition -- individual not-yet-commenced provisions (if any) are noted
    in the Act's own endnotes deeper in the document and are a per-provision
    extraction concern, not something this scraper resolves.
    """
    lines = [
        "[FRL COMPILATION METADATA -- parsed deterministically by the scraper, not LLM-inferred]",
        f"Act: {target.act_name}",
        f"Provision of interest for this volume: {target.provision_of_interest}",
        f"Compilation No.: {metadata['compilation_no']}",
        f"Compilation date (commencement of this compilation): {metadata['compilation_date']}",
        f"Includes amendments up to: {metadata['includes_amendments']}",
        f"Authorised Version: {metadata['authorised_version_id']} "
        f"(registered {metadata['authorised_version_registered']})",
        "Status: in_force (current published compilation of a principal Act; any provision-specific "
        "'not yet commenced' notes are in the Act's own endnotes, not resolved here)",
        f"Source URL: {_build_pdf_url(target)}",
        f"Licence: CC BY 4.0. {ATTRIBUTION_TEMPLATE.format(retrieved_date=retrieved_date.isoformat())}",
        "[END METADATA]",
        "",
    ]
    return "\n".join(lines)


def scrape_legislation_frl(session: Session) -> list[Source]:
    sources = []
    for target in TARGETS:
        url = _build_pdf_url(target)
        content = fetch(url)
        content_hash = compute_content_hash(content)

        filename = f"{target.act_id}_vol{target.volume}_{target.compilation_date}.pdf"
        dest_path = DEST_DIR / filename
        save_raw_file(content, dest_path)

        full_text = extract_text(content, url)
        # front matter is always on page 1 -- comfortably within the first 2000 chars
        metadata = parse_compilation_metadata(full_text[:2000])

        retrieved_date = date.today()
        annotated_content = build_metadata_block(target, metadata, retrieved_date) + full_text

        source = Source(
            entity_name=target.act_name,
            parent_entity=None,
            source_tier="statute",
            url=url,
            retrieved_date=retrieved_date,
            raw_file_path=str(dest_path),
            raw_content=annotated_content,
            content_hash=content_hash,
        )
        session.add(source)
        session.flush()
        sources.append(source)
    return sources


# --- Change detection ---
#
# Recommended cadence: weekly. Acts change less often than lender T&Cs, but a
# hash check is cheap regardless, and legislation's commencement dates
# (4d) are precise -- minimizing the lag between a provision actually
# commencing and our database reflecting it is worth a tighter cadence than
# the monthly one used for lenders, even though most weekly checks will find
# nothing.
#
# IMPORTANT LIMITATION: this only detects a changed hash at the CURRENTLY
# PINNED url (TARGETS' compilation_date is a hardcoded string -- see module
# docstring on why there's no reliable "latest" URL to poll instead). It
# cannot by itself discover that a brand-new compilation now exists at a
# different, not-yet-pinned URL. That's why check_legislation_targets() also
# emits a periodic reminder (see below) to manually verify TARGETS against
# the Register, independent of whether any hash changed this run.

REMINDER_INTERVAL_DAYS = 90
REMINDER_STATE_FILE = Path("logs/.legislation_reminder_state.json")


def _should_emit_quarterly_reminder() -> bool:
    if not REMINDER_STATE_FILE.exists():
        return True
    try:
        last = date.fromisoformat(json.loads(REMINDER_STATE_FILE.read_text())["last_reminder"])
    except (OSError, ValueError, KeyError):
        return True
    return (date.today() - last).days >= REMINDER_INTERVAL_DAYS


def _record_quarterly_reminder() -> None:
    REMINDER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    REMINDER_STATE_FILE.write_text(json.dumps({"last_reminder": date.today().isoformat()}))


def check_legislation_targets(session: Session, *, run_extraction: bool = True) -> list["CheckResult"]:
    """
    Weekly-cadence change check for every entry in TARGETS: fetch, hash, and
    only persist + extract if the hash differs from the most recent stored
    source for that same (act, url) pair. Also surfaces the quarterly
    "verify TARGETS are still current" reminder described above.
    """
    from ingestion.change_detection import CheckResult, get_latest_source
    from ingestion.extraction.extract_rules import ExtractionFailedError, extract_candidates

    results = []
    for target in TARGETS:
        url = _build_pdf_url(target)
        try:
            content = fetch(url)
        except Exception as e:
            logger.error("ERROR         entity=%s url=%s -- %s: %s", target.act_name, url, type(e).__name__, e)
            results.append(CheckResult(target.act_name, url, "error", error=str(e)))
            continue

        content_hash = compute_content_hash(content)
        previous = get_latest_source(session, target.act_name, url)

        if previous is not None and previous.content_hash == content_hash:
            logger.info(
                "UNCHANGED     entity=%s url=%s (matches source id=%s from %s)",
                target.act_name, url, previous.id, previous.retrieved_date,
            )
            results.append(CheckResult(target.act_name, url, "unchanged", source=previous))
            continue

        filename = f"{target.act_id}_vol{target.volume}_{target.compilation_date}.pdf"
        dest_path = DEST_DIR / filename
        save_raw_file(content, dest_path)

        full_text = extract_text(content, url)
        metadata = parse_compilation_metadata(full_text[:2000])
        retrieved_date = date.today()
        annotated_content = build_metadata_block(target, metadata, retrieved_date) + full_text

        source = Source(
            entity_name=target.act_name,
            parent_entity=None,
            source_tier="statute",
            url=url,
            retrieved_date=retrieved_date,
            raw_file_path=str(dest_path),
            raw_content=annotated_content,
            content_hash=content_hash,
        )
        session.add(source)
        session.flush()

        status = "new" if previous is None else "changed"
        logger.info("%-13s entity=%s url=%s new source id=%s", status.upper(), target.act_name, url, source.id)

        candidates_created = 0
        extraction_status = None
        if run_extraction:
            try:
                candidates = extract_candidates(session, source)
                candidates_created = len(candidates)
                extraction_status = "ok"
                logger.info(
                    "EXTRACTED     entity=%s source id=%s -> %d candidate(s) ready for review",
                    target.act_name, source.id, candidates_created,
                )
            except ExtractionFailedError as e:
                extraction_status = "extraction_failed_after_retries"
                logger.error(
                    "EXTRACTION_FAILED_AFTER_RETRIES  entity=%s source id=%s -- %s",
                    target.act_name, source.id, e,
                )
            except Exception as e:
                extraction_status = "extraction_error"
                logger.error(
                    "EXTRACTION FAILED  entity=%s source id=%s -- %s: %s",
                    target.act_name, source.id, type(e).__name__, e,
                )

        session.commit()
        results.append(
            CheckResult(
                target.act_name, url, status, source=source,
                candidates_created=candidates_created, extraction_status=extraction_status,
            )
        )

    if _should_emit_quarterly_reminder():
        pinned = ", ".join(f"{t.act_name} ({t.compilation_date})" for t in TARGETS)
        logger.warning(
            "QUARTERLY REMINDER  legislation TARGETS are pinned to specific compilation dates and "
            "cannot self-update (no reliable 'latest' URL exists for this site -- see module docstring). "
            "Manually verify these are still the current compilations on legislation.gov.au and update "
            "TARGETS in ingestion/scrapers/legislation_frl.py if not: %s",
            pinned,
        )
        _record_quarterly_reminder()

    return results


if __name__ == "__main__":
    from db.session import get_session

    session = get_session()
    inserted = scrape_legislation_frl(session)
    session.commit()
    for source in inserted:
        print(f"source id={source.id} entity={source.entity_name} url={source.url} hash={source.content_hash}")
