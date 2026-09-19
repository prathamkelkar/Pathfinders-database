"""
v1 general extraction for sources the original Phase-1/2 pass never covered.

Ordered by value-per-call, not by id: Pepper 15 is a 69k-char Retail Product Guide
with known serviceability and overtime content that has yielded exactly one
candidate, because only the targeted v2 break-cost pass has ever been run over it.

Commits after EVERY source -- extract_candidates() flushes but does not commit,
and a multi-document LLM run that commits only at the end loses everything it paid
for if the process dies.
"""
import logging

from db.session import get_session
from db.models import Source
from ingestion.extraction.extract_rules import ExtractionFailedError, extract_candidates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")

SOURCE_IDS = [15, 55, 56, 57, 29, 30]  # Pepper guide, Afterpay terms x3, APRA x2

session = get_session()
failures: list[tuple] = []
for sid in SOURCE_IDS:
    src = session.get(Source, sid)
    print(f"\n--- id={sid} {src.entity_name} ({len(src.raw_content):,} chars) ---", flush=True)
    try:
        created = extract_candidates(session, src)
    # Deliberately broad. This is a long unattended run over expensive calls, and
    # one source failing must never cost the others their turn -- a single read
    # timeout on Pepper Money 15 previously aborted the script before Afterpay and
    # APRA were attempted at all. Failures are reported per source and the run
    # continues; nothing is silently swallowed.
    except Exception as e:  # noqa: BLE001
        session.rollback()
        print(f"    FAILED ({type(e).__name__}): {e}", flush=True)
        failures.append((sid, src.entity_name, type(e).__name__))
        continue
    session.commit()
    print(f"    -> {len(created)} candidate(s) committed", flush=True)

print("\n=== SUMMARY ===")
if failures:
    for sid, name, err in failures:
        print(f"  FAILED id={sid} {name}: {err}")
else:
    print("  all sources completed")
