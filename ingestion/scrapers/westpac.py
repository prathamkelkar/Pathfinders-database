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
    # Added after the targeted break-cost/discharge pass found ZERO break-cost
    # vocabulary in the two documents above -- correctly so: they're Personal Loan
    # and Flexi Loan terms, and personal loans don't carry fixed-rate break costs.
    # Westpac AU does not publish its home loan contract terms as a public PDF (the
    # contract is issued in the loan offer pack; its /terms-conditions hub links no
    # mortgage T&Cs, and the Premier Advantage Package conditions booklet was checked
    # and contains no break-cost or discharge vocabulary at all). These two
    # server-rendered pages are Westpac's own substantive public statement of home
    # loan break-cost policy -- 77 and 52 keyword hits respectively.
    "https://www.westpac.com.au/personal-banking/home-loans/manage-home-loan/break-cost/what-is-a-break-cost/",
    "https://www.westpac.com.au/personal-banking/home-loans/manage-home-loan/break-cost/",
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
