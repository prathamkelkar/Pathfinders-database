"""
Scrapers for the approved top-10 non-bank lender/BNPL shortlist (Task 3),
minus Resimac (see NOT SCRAPED below).

Grouped into one file because every one of these lenders publishes the same
*kind* of source: a flat set of PDF/HTML documents (T&Cs, Credit Guide,
Product Guide), fetched exactly the way ingestion/scrapers/cba.py and the
other Big 4 scrapers already do. There's no legislation-style compilation/
versioning structure here (that's what ingestion/scrapers/legislation_frl.py
handles), so ten near-identical files each defining one ENTITY_NAME and one
DOCUMENT_URLS list would just be duplication with no real difference. Each
lender still gets its own thin, independently-callable, independently-
testable scrape_<lender>(session) function -- only the fetch/save/hash
orchestration is shared, via base.scrape_documents().

NOT SCRAPED -- Resimac:
Both resimac.com.au and broker.resimac.com.au are behind Incapsula bot
protection and return only a JS challenge page for automated requests, for
every URL in the source inventory (the /disclosures page and the broker
product-specs PDF alike). No scraper was written for it -- this project does
not attempt to bypass bot protection. Getting Resimac's documents would
require a broker portal login (manual, out of scope) or an approved API/data
arrangement.

NOT SCRAPED -- Latitude Financial:
Every legal/T&Cs PDF Latitude publishes (confirmed via their own "Terms and
Conditions Library" page) is hosted on assets.latitudefinancial.com, whose
robots.txt disallows all bot access site-wide ("Disallow: /"). Their main site
(www.latitudefinancial.com.au) allows bots, but hosts no documents itself --
every link on it points back to the disallowed asset host. No scraper was
written for it, for the same reason as Resimac: this project respects
robots.txt rather than bypassing it. Confirmed there's no accessible
alternative host for the same documents.

SERVICEABILITY POLICY FINDING (confirms CONTEXT.md's core premise): none of
the documents scraped below contain an actual serviceability/credit-
assessment methodology. What's public is contractual T&Cs, NCCP-mandated
Credit Guides, or -- for Liberty specifically -- a page literally titled
"Responsible Lending Policy" that turns out to be generic NCCP-obligation
boilerplate with no assessment criteria at all. The one partial exception is
Pepper Money's "Servicing and additional Lending Policies" document, which has
some real income/servicing content (still no HECS-specific clause). Every
lender scraped here is therefore a Layer 3 candidate for its *actual*
serviceability treatment (ingestion/layer3_intake.py) -- this scraper only
populates the structured-queryability (T&Cs/Credit Guide) layer, per
CONTEXT.md section 2. No Layer 3 entries were fabricated here: we don't have
real broker-sourced knowledge of any of these lenders' undocumented policies,
unlike the CBA HECS case.
"""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from db.models import Source
from ingestion.scrapers.base import scrape_documents


@dataclass(frozen=True)
class LenderConfig:
    entity_name: str
    urls: tuple[str, ...]
    dest_dir: Path
    parent_entity: str | None = None


LENDERS: dict[str, LenderConfig] = {
    "latrobe": LenderConfig(
        entity_name="La Trobe Financial",
        urls=(
            "https://www.latrobefinancial.com.au/app/uploads/Information-Summary-TCs.pdf",
            "https://www.latrobefinancial.com.au/wp-content/uploads/Qantas_Broker_Product_Guide.pdf",
            "https://www.latrobefinancial.com.au/app/uploads/financial-services-guide-1.pdf",
        ),
        dest_dir=Path("sources/latrobe"),
    ),
    "pepper": LenderConfig(
        entity_name="Pepper Money",
        urls=(
            "https://www.pepperbroker.com.au/content/dam/aubroker/broker-au-documents-nc/Pepper%20Money%20Retail%20Product%20Guide.pdf",
            "https://www.pepperbroker.com.au/content/dam/aubroker/broker-au-documents-nc/home-loans/product-guide/sectioned/Servicing%20and%20additional%20HOME%20LOAN%20Lending%20Policies%20(pages%2012-14).pdf",
        ),
        dest_dir=Path("sources/pepper"),
    ),
    "firstmac": LenderConfig(
        entity_name="Firstmac",
        # Deposit PDSs doc excluded -- it's for term deposits, not home loans/credit.
        # FAQs added after the targeted break-cost/discharge pass found zero relevant
        # vocabulary in the FSG (an FSG is a licensing disclosure, not loan terms).
        # Firstmac's actual loan terms -- "Your Loan General Terms and Conditions" and
        # "Your Mortgage Common Provisions", named on their own loan-agreement page --
        # are NOT published publicly; they're issued with the loan offer pack, and
        # their loan-docs microsite only hosts printing/how-to helper guides. The FAQs
        # are thin on this (one break-fee and one payout-figure mention) but are the
        # only public Firstmac page that addresses it at all.
        urls=(
            "https://www.firstmac.com.au/media/docs/financial-services-guide.pdf",
            "https://www.firstmac.com.au/faqs",
        ),
        dest_dir=Path("sources/firstmac"),
    ),
    "moneyme": LenderConfig(
        entity_name="MoneyMe",
        urls=(
            "https://mmestoragecdn.blob.core.windows.net/web2/v3/images/legal/credit-guide.pdf",
            "https://cdn.moneyme.com.au/info/tmd/s1-tmd-secured-pl-v2.pdf",
        ),
        dest_dir=Path("sources/moneyme"),
    ),
    "wisr": LenderConfig(
        entity_name="Wisr",
        # No T&Cs/policy PDF found anywhere on wisr.com.au (Task 3 finding) --
        # the Credit Guide is only published as an HTML page.
        urls=("https://wisr.com.au/credit-guide",),
        dest_dir=Path("sources/wisr"),
    ),
    "liberty": LenderConfig(
        entity_name="Liberty Financial",
        # The responsible-lending page is generic NCCP boilerplate, not loan terms --
        # the targeted break-cost/discharge pass correctly found nothing in it.
        # FAQs added as the correction: 17 break-cost hits (including Liberty's own
        # "economic cost" framing) and 3 discharge hits. Liberty AU does not publish
        # its home loan contract terms publicly. NOTE: a "Liberty MORTGAGE LOAN
        # AGREEMENT SPECIFIC TERMS" PDF does surface in search, but it is Liberty
        # Financial NEW ZEALAND (Auckland address, libfin.co.nz) -- a different
        # entity, deliberately NOT used here, same trap as the Westpac/ANZ NZ docs.
        urls=(
            "https://www.liberty.com.au/disclosures/responsible-lending",
            "https://www.liberty.com.au/about-us/faqs",
        ),
        dest_dir=Path("sources/liberty"),
    ),
    "afterpay": LenderConfig(
        entity_name="Afterpay",
        parent_entity="Block, Inc.",
        urls=("https://www.afterpay.com/en-AU/terms-of-service",),
        dest_dir=Path("sources/afterpay"),
    ),
    "zip": LenderConfig(
        entity_name="Zip Co",
        urls=(
            "https://zip.co/files/au/zip-pay-terms-and-conditions.pdf",
            "https://zip.co/files/au/zip-plus-terms-and-conditions.pdf",
            "https://zip.co/files/au/zip-money-terms-and-conditions.pdf",
        ),
        dest_dir=Path("sources/zip"),
    ),
}


def scrape_lender(session: Session, key: str) -> list[Source]:
    config = LENDERS[key]
    return scrape_documents(
        session,
        entity_name=config.entity_name,
        parent_entity=config.parent_entity,
        urls=list(config.urls),
        dest_dir=config.dest_dir,
        source_tier="lender_official",
    )


def scrape_latrobe(session: Session) -> list[Source]:
    return scrape_lender(session, "latrobe")


def scrape_pepper(session: Session) -> list[Source]:
    return scrape_lender(session, "pepper")


def scrape_firstmac(session: Session) -> list[Source]:
    return scrape_lender(session, "firstmac")


def scrape_moneyme(session: Session) -> list[Source]:
    return scrape_lender(session, "moneyme")


def scrape_wisr(session: Session) -> list[Source]:
    return scrape_lender(session, "wisr")


def scrape_liberty(session: Session) -> list[Source]:
    return scrape_lender(session, "liberty")


def scrape_afterpay(session: Session) -> list[Source]:
    return scrape_lender(session, "afterpay")


def scrape_zip(session: Session) -> list[Source]:
    return scrape_lender(session, "zip")


ALL_SCRAPERS = {
    "latrobe": scrape_latrobe,
    "pepper": scrape_pepper,
    "firstmac": scrape_firstmac,
    "moneyme": scrape_moneyme,
    "wisr": scrape_wisr,
    "liberty": scrape_liberty,
    "afterpay": scrape_afterpay,
    "zip": scrape_zip,
}


if __name__ == "__main__":
    from db.session import get_session

    session = get_session()
    for key, scrape_fn in ALL_SCRAPERS.items():
        print(f"--- Scraping {key} ---")
        try:
            inserted = scrape_fn(session)
            session.commit()
            for source in inserted:
                print(f"  source id={source.id} url={source.url} hash={source.content_hash}")
        except Exception as e:
            session.rollback()
            print(f"  FAILED: {type(e).__name__}: {e}")
