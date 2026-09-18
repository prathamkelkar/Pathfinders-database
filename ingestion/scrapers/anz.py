"""
ANZ scraper. URLs sourced from ingestion/source_inventory.md.

Only one document URL: the source inventory research found no second
ANZ-specific PDF (the "Fees & terms" page is a link-index hub, not a document
itself, so it's excluded here -- same reasoning CBA's scraper applies by only
listing actual document URLs, not hub/index pages).
"""

from pathlib import Path

from sqlalchemy.orm import Session

from ingestion.scrapers.base import scrape_documents

ENTITY_NAME = "ANZ"
PARENT_ENTITY = None
SOURCE_TIER = "lender_official"

DOCUMENT_URLS = [
    "https://www.anz.com.au/content/dam/anzcomau/documents/pdf/consumer-lending-tc.pdf",
]

DEST_DIR = Path("sources/anz")


def scrape_anz(session: Session):
    return scrape_documents(
        session,
        entity_name=ENTITY_NAME,
        parent_entity=PARENT_ENTITY,
        urls=DOCUMENT_URLS,
        dest_dir=DEST_DIR,
        source_tier=SOURCE_TIER,
    )


if __name__ == "__main__":
    from db.session import get_session

    session = get_session()
    inserted = scrape_anz(session)
    session.commit()
    for source in inserted:
        print(f"source id={source.id} url={source.url} hash={source.content_hash}")
