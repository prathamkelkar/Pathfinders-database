"""
Jurisdiction guard: refuses to ingest New Zealand lender material as Australian.

Why this exists. Three of the lenders this project tracks have New Zealand
operations publishing near-identical documents, and NZ material has come within
one step of our database three separate times:

  - ANZ NZ rental-income shading (ANZ's legal name is "Australia and NEW ZEALAND
    Banking Group", so the brand name itself is not a jurisdiction signal)
  - Westpac NZ home loan terms and conditions
  - A "Liberty MORTGAGE LOAN AGREEMENT" that was Liberty Financial New Zealand

The most dangerous instance found so far was three "ANZ Home Loan Product and
Policy Guide" PDFs hosted on an AUSTRALIAN broker-CRM S3 bucket
(prod-mycrm-static-documents, ap-southeast-2). Nothing in the domain, the path or
the title said New Zealand; they ranked for Australian broker-policy searches; and
their contents were ANZ Bank New Zealand Limited. They stated overtime shading of
80% -- coincidentally identical to ANZ Australia -- but derived it from a
three-month averaging method that is not ANZ Australia's. Ingesting them would
have produced policy that looked right and was wrong, which is worse than a gap.

Domain filtering alone is NOT sufficient: anz.com.au serves NZ content under
/newsroom/new-zealand/. This checks document CONTENT, which is what actually
determines jurisdiction.
"""

import logging
import re

logger = logging.getLogger("ingestion.jurisdiction")

# Terms that only appear in New Zealand financial documents. Chosen to be
# unambiguous -- "Auckland" or "Wellington" alone could appear in an Australian
# document discussing a NZ subsidiary, so they are weak signals below, not here.
NZ_DECISIVE_MARKERS = (
    "anz bank new zealand",
    "westpac new zealand",
    "reserve bank of new zealand",
    "kiwisaver",
    "low equity premium",
    "work and income new zealand",
    "inland revenue department",
    "financial markets authority (nz)",
    ".co.nz",
    "/new-zealand/",
)

# Present in NZ documents but individually explainable in an Australian one.
NZ_WEAK_MARKERS = (
    "auckland", "wellington", "christchurch", "nzd", "atainz",
    "student loan repayment", "rbnz",
)

# Positive Australian evidence. Their ABSENCE is not proof of NZ, but their
# presence alongside weak NZ markers usually means an Australian document that
# merely mentions New Zealand.
AU_MARKERS = (
    "australian credit licence", "afsl", "abn ", "acn ",
    "australian taxation office", "ato", "apra", "asic",
    "hecs", "help debt", "lenders mortgage insurance", "lmi",
    "australian securities", "ppsr",
)


class NonAustralianSourceError(ValueError):
    """Raised when a document is identified as New Zealand rather than Australian."""


def _count(text: str, markers: tuple[str, ...]) -> dict[str, int]:
    return {m: text.count(m) for m in markers if m in text}


def check_jurisdiction(text: str, url: str | None = None) -> dict:
    """
    Classify a document as "australian", "new_zealand", or "uncertain".

    Deliberately returns a verdict rather than a bool, because "uncertain" is a
    real and useful third state -- a short marketing page may carry no
    jurisdiction evidence either way, and silently treating that as Australian is
    how the ANZ NZ guides nearly got in.
    """
    haystack = f"{url or ''}\n{text}".lower()

    decisive = _count(haystack, NZ_DECISIVE_MARKERS)
    weak = _count(haystack, NZ_WEAK_MARKERS)
    au = _count(haystack, AU_MARKERS)

    if decisive:
        verdict = "new_zealand"
        reason = f"decisive NZ markers present: {decisive}"
    elif weak and not au:
        verdict = "new_zealand"
        reason = f"NZ markers with no Australian markers: {weak}"
    elif weak and au:
        # e.g. an Australian document discussing a NZ subsidiary, or ANZ's own
        # legal name. Flagged for a human rather than resolved automatically.
        verdict = "uncertain"
        reason = f"both NZ markers {weak} and Australian markers {sorted(au)} present"
    elif au:
        verdict = "australian"
        reason = f"Australian markers present: {sorted(au)}"
    else:
        verdict = "uncertain"
        reason = "no jurisdiction markers found either way"

    return {"verdict": verdict, "reason": reason,
            "nz_decisive": decisive, "nz_weak": weak, "au_markers": au}


def assert_australian(text: str, url: str | None = None) -> dict:
    """
    Raise NonAustralianSourceError if the document is New Zealand; log a warning
    if uncertain. Uncertain is deliberately NOT fatal -- short Australian pages
    legitimately carry no markers, and blocking them would make the guard
    unusable, which is how guards get switched off.
    """
    result = check_jurisdiction(text, url)
    if result["verdict"] == "new_zealand":
        raise NonAustralianSourceError(
            f"Refusing to ingest: document appears to be New Zealand, not Australian "
            f"({result['reason']}). url={url!r}"
        )
    if result["verdict"] == "uncertain":
        logger.warning("JURISDICTION UNCERTAIN url=%s -- %s", url, result["reason"])
    return result
