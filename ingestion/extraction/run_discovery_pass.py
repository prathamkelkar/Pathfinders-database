import logging
from db.session import get_session
from ingestion.extraction.discover_vocabulary import (
    discover_for_source, find_suspicious_absences, novel_phrases,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
session = get_session()

suspicious = find_suspicious_absences(session)
print(f"{len(suspicious)} suspicious (source, topic) pairs to audit\n", flush=True)

results = []
for source, topic in suspicious:
    print(f"--- id={source.id} {source.entity_name} {topic} ({len(source.raw_content):,} chars) ---", flush=True)
    results.append(discover_for_source(source, topic))

print("\n\n=== VERIFIED PHRASINGS FOUND ===")
for r in results:
    if r.verified:
        print(f"\n{r.lender} id={r.source_id} [{r.topic}]")
        for v in r.verified:
            print(f'   "{v["phrase"]}"')
            print(f'      quote: {str(v.get("quote",""))[:190]}')
    elif r.error:
        print(f"\n{r.lender} id={r.source_id} [{r.topic}] ERROR: {r.error[:120]}")

print("\n\n=== UNVERIFIED (model did not copy verbatim -- rejected) ===")
for r in results:
    for u in r.unverified:
        print(f'  {r.lender} id={r.source_id} [{r.topic}] "{u["phrase"]}"')

print("\n\n=== NOVEL PHRASES NOT IN TOPIC_KEYWORDS ===")
for topic, phrases in novel_phrases(results).items():
    print(f"\n{topic}:")
    for p in sorted(phrases):
        print(f'    "{p}"')
