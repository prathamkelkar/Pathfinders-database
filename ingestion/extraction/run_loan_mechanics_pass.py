"""
Runner for the targeted break-cost/discharge re-extraction pass (prompt version
"2") over every already-scraped lender_official source.

Run:  python3 -m ingestion.extraction.run_loan_mechanics_pass

Prints a per-lender coverage report distinguishing, per topic:
  found                 -- rules extracted from this document
  present_not_extracted -- the document discusses the topic but nothing
                           structured came out (needs a human look, NOT a re-scrape)
  absent                -- the document contains no vocabulary for the topic at
                           all, so this information genuinely isn't in the source
                           we hold (this is the only state that justifies new scraping)
  extraction_failed     -- the model never returned usable JSON after retries
"""

import logging
from collections import defaultdict

from db.models import Source
from db.session import get_session
from ingestion.extraction.extract_loan_mechanics import extract_loan_mechanics_from_source

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")


def main() -> None:
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
        results.append(extract_loan_mechanics_from_source(session, source))

    # Roll up per lender: a lender counts as covered for a topic if ANY of its
    # documents yielded rules for it.
    by_lender: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in results:
        for topic, tr in r.topics.items():
            by_lender[r.lender][topic].append(tr)

    print("\n\n=== PER-LENDER COVERAGE ===")
    for lender in sorted(by_lender):
        print(f"\n{lender}")
        for topic in ("break_cost", "discharge"):
            topic_results = by_lender[lender].get(topic, [])
            if not topic_results:
                continue
            created = sum(t.candidates_created for t in topic_results)
            coverages = [t.coverage for t in topic_results]
            if "found" in coverages:
                verdict = f"FOUND ({created} candidate(s))"
            elif "extraction_failed" in coverages:
                verdict = "EXTRACTION FAILED -- retry, do not conclude anything"
            elif "present_not_extracted" in coverages:
                verdict = "PRESENT BUT NOT EXTRACTED -- topic discussed, nothing structured came out"
            else:
                verdict = "GENUINELY ABSENT from the sources we hold -> candidate for new scraping"
            hits = sum(t.keyword_hits for t in topic_results)
            print(f"  {topic:<12} {verdict}  (keyword hits across {len(topic_results)} doc(s): {hits})")


if __name__ == "__main__":
    main()
