"""
Westpac scraper. URLs sourced from ingestion/source_inventory.md.
"""

from pathlib import Path

from sqlalchemy.orm import Session

from ingestion.scrapers.base import scrape_documents

ENTITY_NAME = "Westpac"
PARENT_ENTITY = None
SOURCE_TIER = "lender_official"

DOCUMENT_URLS = [
    "https://www.westpac.com.au/content/dam/public/wbc/documents/pdf/pb/Flexi_Loan_Conditions.pdf",
    "https://www.westpac.com.au/content/dam/public/wbc/documents/pdf/pb/personal-loans/p-l-contract-general-conditions-180324.pdf",
]

DEST_DIR = Path("sources/westpac")


def scrape_westpac(session: Session):
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
    inserted = scrape_westpac(session)
    session.commit()
    for source in inserted:
        print(f"source id={source.id} url={source.url} hash={source.content_hash}")
