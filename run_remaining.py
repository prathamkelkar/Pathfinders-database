import logging
from db.session import get_session
from db.models import Source
from ingestion.extraction.extract_rules import extract_candidates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
session = get_session()
for sid in (83, 82):  # Bluestone then Macquarie (smallest first)
    src = session.get(Source, sid)
    print(f"\n--- id={sid} {src.entity_name} ({len(src.raw_content):,} chars) ---", flush=True)
    try:
        created = extract_candidates(session, src)
    except Exception as e:
        session.rollback()
        print(f"    FAILED ({type(e).__name__}): {str(e)[:150]}", flush=True)
        continue
    session.commit()
    print(f"    -> {len(created)} candidate(s) committed", flush=True)
print("\n=== DONE ===")
