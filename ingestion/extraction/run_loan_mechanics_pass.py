"""
Runner for the targeted break-cost/discharge re-extraction pass (prompt version
"2") over every already-scraped lender_official source.

Run:  python3 -m ingestion.extraction.run_loan_mechanics_pass

Prints a per-lender coverage report distinguishing, per topic:
  found                 -- rules extracted from this document
  already_extracted     -- a previous (interrupted) run already stored version-2
                           candidates for this topic; skipped, not re-asked
  present_not_extracted -- the document discusses the topic but nothing
                           structured came out (needs a human look, NOT a re-scrape)
  absent                -- no vocabulary for the topic matched in this document.
                           NOTE this is a statement about our keyword list, not
                           about the document: it is only as good as TOPIC_KEYWORDS,
                           and a missing phrasing produces a confident false
                           negative. Afterpay's Pay Monthly terms scanned as absent
                           across 181k chars until "early payment" was added to the
                           vocabulary. Treat absent on a large document as suspect.
  extraction_failed     -- the model never returned usable JSON after retries
"""

import argparse
import logging
from collections import defaultdict

from db.models import CandidateRule, Source
from db.session import get_session
from ingestion.extraction.extract_loan_mechanics import (
    LOAN_MECHANICS_PROMPT_VERSION,
    extract_loan_mechanics_from_source,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")


# Above this size, an "absent" verdict is reported as suspicious rather than
# accepted. Chosen from the corpus: every document that genuinely had nothing to
# say about loan mechanics was small (a credit guide, a nav page), while every
# false absent found so far was large.
SUSPICIOUS_ABSENT_CHARS = 20000


def stored_candidate_count(session, source_id: int, policy_area: str) -> int:
    """Version-2 candidates already on disk, so a resumed run reports the real total."""
    return (
        session.query(CandidateRule)
        .filter(
            CandidateRule.source_id == source_id,
            CandidateRule.extraction_prompt_version == LOAN_MECHANICS_PROMPT_VERSION,
            CandidateRule.policy_area == policy_area,
        )
        .count()
    )


def main(resume: bool = True) -> None:
    session = get_session()
    sources = (
        session.query(Source)
        .filter(Source.source_tier == "lender_official")
        .order_by(Source.entity_name, Source.id)
        .all()
    )

    results = []
    for source in sources:
        print(f"\n--- source id={source.id} {source.entity_name} ({len(source.raw_content or '')} chars) ---", flush=True)
        results.append(extract_loan_mechanics_from_source(session, source, resume=resume))

    # Roll up per lender: a lender counts as covered for a topic if ANY of its
    # documents yielded rules for it.
    by_lender: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    by_lender_source_ids: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    by_lender_source_sizes: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    source_sizes = {s.id: len(s.raw_content or "") for s in sources}
    for r in results:
        for topic, tr in r.topics.items():
            by_lender[r.lender][topic].append(tr)
            by_lender_source_ids[r.lender][topic].append(r.source_id)
            by_lender_source_sizes[r.lender][topic].append(source_sizes.get(r.source_id, 0))

    print("\n\n=== PER-LENDER COVERAGE ===")
    for lender in sorted(by_lender):
        print(f"\n{lender}")
        for topic in ("break_cost", "discharge"):
            topic_results = by_lender[lender].get(topic, [])
            if not topic_results:
                continue
            coverages = [t.coverage for t in topic_results]
            # Candidates stored by an earlier interrupted run count toward the total,
            # otherwise a resumed run under-reports its own results. Without this,
            # "already_extracted" would fall through to the final else branch and be
            # reported as GENUINELY ABSENT -- the exact opposite of the truth, and the
            # one verdict that sends us scraping for something we already hold.
            created = sum(
                t.candidates_created
                if t.coverage != "already_extracted"
                else stored_candidate_count(session, tr_source_id, topic)
                for t, tr_source_id in zip(topic_results, by_lender_source_ids[lender][topic])
            )
            if "found" in coverages or "already_extracted" in coverages:
                verdict = f"FOUND ({created} candidate(s))"
            elif "extraction_failed" in coverages:
                verdict = "EXTRACTION FAILED -- retry, do not conclude anything"
            elif "present_not_extracted" in coverages:
                verdict = "PRESENT BUT NOT EXTRACTED -- topic discussed, nothing structured came out"
            else:
                # Deliberately NOT phrased as "genuinely absent". The scan cannot
                # support that claim -- it can only report that nothing in
                # TOPIC_KEYWORDS matched. Overstating it here is what would send
                # someone scraping for a document we already hold, or worse, let
                # them record a real policy as non-existent.
                verdict = "NO VOCABULARY MATCHED -- unconfirmed; verify before treating as absent"
            hits = sum(t.keyword_hits for t in topic_results)
            print(f"  {topic:<12} {verdict}  (keyword hits across {len(topic_results)} doc(s): {hits})")

            # A large document that matched nothing is the highest-risk verdict in
            # this report: big documents are the ones that DO discuss loan mechanics,
            # so silence from the scanner more likely means a vocabulary gap than an
            # actual gap in the document.
            unmatched_large = [
                (t, sid, size)
                for t, sid, size in zip(topic_results, by_lender_source_ids[lender][topic],
                                        by_lender_source_sizes[lender][topic])
                if t.coverage == "absent" and size > SUSPICIOUS_ABSENT_CHARS
            ]
            for _, sid, size in unmatched_large:
                print(f"      !! suspicious: source id={sid} is {size:,} chars and matched nothing")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Targeted break-cost/discharge re-extraction pass.")
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Re-extract every topic even where version-2 candidates already exist. "
        "Off by default: a plain re-run resumes, so an interrupted pass never pays "
        "for the same LLM call twice.",
    )
    args = parser.parse_args()
    main(resume=not args.no_resume)
