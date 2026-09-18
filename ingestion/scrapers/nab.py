"""
NAB scraper. URLs sourced from ingestion/source_inventory.md.
"""

from pathlib import Path

from sqlalchemy.orm import Session

from ingestion.scrapers.base import scrape_documents

ENTITY_NAME = "NAB"
PARENT_ENTITY = None
SOURCE_TIER = "lender_official"

DOCUMENT_URLS = [
    "https://www.nab.com.au/content/dam/nabrwd/documents/terms-and-conditions/loans/home-loan-general-terms.pdf",
    "https://www.nab.com.au/content/dam/nabrwd/documents/terms-and-conditions/loans/nab-choice-package-terms-conditions.pdf",
]

DEST_DIR = Path("sources/nab")


def scrape_nab(session: Session):
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
    inserted = scrape_nab(session)
    session.commit()
    for source in inserted:
        print(f"source id={source.id} url={source.url} hash={source.content_hash}")
