"""
APRA scraper: Prudential Practice Guide APG 223 (Residential Mortgage Lending)
and Reporting Standard ARS 223.0 (Residential Mortgage Lending) -- the
regulator_guidance-tier documents CONTEXT.md 9b identifies as the framework
that generalizes the *pattern* behind lender-specific HELP/HECS serviceability
treatment (e.g. the CBA example) across the industry.

Verified 2026-09-18 by downloading both PDFs directly and reading the actual
HELP-debt text (not secondhand summaries -- a general web search on this topic
returned an incorrect claim that HELP debt is *included* in DTI; the real
documents say the opposite, see below):

- APG 223 (practice guide, published June 2025): APRA's baseline expectation is
  that ADIs continue to consider HELP debt in serviceability, since HELP
  repayments reduce income available to service a mortgage. It is reasonable
  (not mandatory) for an ADI to remove HELP repayments from a serviceability
  assessment specifically where the borrower is expected to repay the HELP debt
  within 12 months via compulsory income-contingent repayments -- an ADI-level
  override requiring "prudent frameworks", not an APRA mandate and not a
  specific threshold. This document alone does NOT tell us any individual
  lender's actual threshold (e.g. CBA's tacit 1-year/5-year cutoffs) --
  lender-specific thresholds still require Layer 3 sourcing per lender within
  this permitted ceiling (CONTEXT.md 9b.0).
- ARS 223.0 (reporting standard, applies for reporting periods ending on or
  after 30 September 2025): HELP debts MUST be excluded from the credit limit
  of all debts used to calculate the debt-to-income ratio -- this one IS a
  mandatory exclusion, unconditional, and unrelated to the (optional)
  serviceability override above.

CONTENT LICENCE (checked at apra.gov.au/copyright): Creative Commons
Attribution 4.0 International, except the Commonwealth Coat of Arms, APRA/APRA
Connect logos, and third-party material. Required attribution: "©
Australian Prudential Regulation Authority [year]" -- embedded below.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from db.models import Source
from ingestion.scrapers.base import compute_content_hash, extract_text, fetch, save_raw_file

DEST_DIR = Path("sources/apra")
ENTITY_NAME = "APRA"
SOURCE_TIER = "regulator_guidance"

ATTRIBUTION = "© Australian Prudential Regulation Authority 2026. Licensed under CC BY 4.0."


@dataclass(frozen=True)
class ApraDocument:
    title: str
    url: str
    filename: str
    document_type: str  # "practice_guide" | "reporting_standard"
    published_or_commences: str  # human-readable note on the relevant date


DOCUMENTS = [
    ApraDocument(
        title="Prudential Practice Guide APG 223 Residential Mortgage Lending",
        url="https://www.apra.gov.au/system/files/2025-07/Prudential%20practice%20guide%20APG%20223%20Residential%20Mortgage%20Lending.pdf",
        filename="APG_223_Residential_Mortgage_Lending_2025-06.pdf",
        document_type="practice_guide",
        published_or_commences="Published June 2025 (finalised following Feb 2025 consultation)",
    ),
    ApraDocument(
        title="Reporting Standard ARS 223.0 Residential Mortgage Lending",
        url="https://www.apra.gov.au/sites/default/files/2025-06/Reporting%20Standard%20ARS%20223.0%20Residential%20Mortgage%20Lending%20-%20Clean.pdf",
        filename="ARS_223.0_Residential_Mortgage_Lending_2025-09.pdf",
        document_type="reporting_standard",
        published_or_commences="Applies for reporting periods ending on or after 30 September 2025",
    ),
]


def build_metadata_block(doc: ApraDocument, retrieved_date: date) -> str:
    return "\n".join(
        [
            "[APRA DOCUMENT METADATA]",
            f"Title: {doc.title}",
            f"Document type: {doc.document_type}",
            f"Relevant date: {doc.published_or_commences}",
            "Status: in_force",
            f"Source URL: {doc.url}",
            f"Licence: CC BY 4.0. {ATTRIBUTION} Retrieved {retrieved_date.isoformat()}.",
            "[END METADATA]",
            "",
        ]
    )


def scrape_apra_apg223(session: Session) -> list[Source]:
    sources = []
    for doc in DOCUMENTS:
        content = fetch(doc.url)
        content_hash = compute_content_hash(content)

        dest_path = DEST_DIR / doc.filename
        save_raw_file(content, dest_path)

        full_text = extract_text(content, doc.url)
        retrieved_date = date.today()
        annotated_content = build_metadata_block(doc, retrieved_date) + full_text

        source = Source(
            entity_name=ENTITY_NAME,
            parent_entity=None,
            source_tier=SOURCE_TIER,
            url=doc.url,
            retrieved_date=retrieved_date,
            raw_file_path=str(dest_path),
            raw_content=annotated_content,
            content_hash=content_hash,
        )
        session.add(source)
        session.flush()
        sources.append(source)
    return sources


if __name__ == "__main__":
    from db.session import get_session

    session = get_session()
    inserted = scrape_apra_apg223(session)
    session.commit()
    for source in inserted:
        print(f"source id={source.id} url={source.url} hash={source.content_hash}")
