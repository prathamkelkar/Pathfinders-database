"""v1 general extraction over the newly scraped serviceability sources."""
import logging
from db.session import get_session
from db.models import Source
from ingestion.extraction.extract_rules import extract_candidates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")

# Smallest first, so a failure on the 93k Macquarie guide costs least.
SOURCE_IDS = [84, 81, 73, 76, 79, 77, 80, 78, 75, 74, 83, 82]

session = get_session()
failures = []
for sid in SOURCE_IDS:
    src = session.get(Source, sid)
    print(f"\n--- id={sid} {src.entity_name} ({len(src.raw_content):,} chars) ---", flush=True)
    try:
        created = extract_candidates(session, src)
    except Exception as e:
        session.rollback()
        print(f"    FAILED ({type(e).__name__}): {str(e)[:150]}", flush=True)
        failures.append((sid, src.entity_name, type(e).__name__))
        continue
    session.commit()
    print(f"    -> {len(created)} candidate(s) committed", flush=True)

print("\n=== SUMMARY ===")
print(f"  failures: {failures or 'none'}")
