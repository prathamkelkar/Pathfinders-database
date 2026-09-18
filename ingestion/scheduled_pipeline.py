"""
Entry point meant to be invoked on a schedule (cron, Task Scheduler, or
whatever the project owner sets up -- this script doesn't wire that up
itself). Checks every known source for changes, extracts new candidates only
when something actually changed, and logs every check -- including the
unchanged ones -- so a scheduled run leaves visible proof it executed even
when nothing needed to happen.

Recommended cadence:
- Lender documents: MONTHLY. T&Cs/PDS changes are infrequent and not tied to
  precise dates -- monthly polling is cheap and catches changes promptly
  enough for CONTEXT.md 4a's "cached, not live" model.
- Legislation: WEEKLY. Acts change less often than lender docs, but a hash
  check costs nothing, and legislation's commencement dates (4d) are precise
  -- a tighter cadence minimizes the lag between a provision actually
  commencing and our database reflecting it. See
  ingestion/scrapers/legislation_frl.py's check_legislation_targets() for an
  important limitation: this can only detect a changed hash at the currently
  pinned compilation URL, not discover that a new compilation now exists
  elsewhere -- that's why it also emits a quarterly manual-verification
  reminder, independent of the weekly hash checks.

This script never writes to production_rules. Run
`python3 -m ingestion.review_candidates` manually after a run that logged new
candidates -- promotion always stays a human decision (CONTEXT.md section 6).
"""

import argparse
import logging
from pathlib import Path

from db.session import get_session
from ingestion.change_detection import check_lender_source
from ingestion.scrapers import anz, cba, nab, nonbank_lenders, westpac
from ingestion.scrapers.legislation_frl import check_legislation_targets

LOG_FILE = Path("logs/scraper_pipeline.log")

BIG4_MODULES = [cba, westpac, nab, anz]


def _configure_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
    )


def run_lender_checks(session) -> list:
    results = []
    for module in BIG4_MODULES:
        for url in module.DOCUMENT_URLS:
            results.append(
                check_lender_source(
                    session,
                    entity_name=module.ENTITY_NAME,
                    parent_entity=module.PARENT_ENTITY,
                    url=url,
                    dest_dir=module.DEST_DIR,
                    source_tier=module.SOURCE_TIER,
                )
            )
    for config in nonbank_lenders.LENDERS.values():
        for url in config.urls:
            results.append(
                check_lender_source(
                    session,
                    entity_name=config.entity_name,
                    parent_entity=config.parent_entity,
                    url=url,
                    dest_dir=config.dest_dir,
                    source_tier="lender_official",
                )
            )
    return results


def run_legislation_checks(session) -> list:
    return check_legislation_targets(session)


def _summarize(results: list) -> None:
    unchanged = [r for r in results if r.status == "unchanged"]
    changed = [r for r in results if r.status in ("new", "changed")]
    errors = [r for r in results if r.status == "error"]
    total_candidates = sum(r.candidates_created for r in changed)

    print(
        f"\nChecked {len(results)} source(s): {len(unchanged)} unchanged, "
        f"{len(changed)} new/changed, {len(errors)} error(s)."
    )
    if total_candidates:
        print(
            f"{total_candidates} new candidate(s) created -- run "
            f"`python3 -m ingestion.review_candidates` to review them."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check all sources for changes; extract only what changed.")
    parser.add_argument("--only", choices=["lenders", "legislation", "all"], default="all")
    args = parser.parse_args()

    _configure_logging()
    session = get_session()

    results = []
    if args.only in ("lenders", "all"):
        results += run_lender_checks(session)
    if args.only in ("legislation", "all"):
        results += run_legislation_checks(session)

    _summarize(results)
