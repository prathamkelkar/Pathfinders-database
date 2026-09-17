"""
CBA scraper. URLs sourced from ingestion/source_inventory.md.
"""

from pathlib import Path

from sqlalchemy.orm import Session

from ingestion.scrapers.base import scrape_documents

ENTITY_NAME = "CBA"
PARENT_ENTITY = None
SOURCE_TIER = "lender_official"

DOCUMENT_URLS = [
    "https://www.commbank.com.au/content/dam/commbank/personal/apply-online/download-printed-forms/utc-home-loan.pdf",
    "https://www.commbank.com.au/content/dam/commbank-assets/home-loans/docs/commbank-home-loan-customer-guide.pdf",
]

DEST_DIR = Path("sources/cba")


def scrape_cba(session: Session):
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
    inserted = scrape_cba(session)
    session.commit()
    for source in inserted:
        print(f"source id={source.id} url={source.url} hash={source.content_hash}")
