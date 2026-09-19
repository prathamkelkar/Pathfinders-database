"""
Tests for the NZ jurisdiction guard.

Each case below is drawn from material this project actually encountered, not
invented. The ANZ case is the important one: the brand name contains "New
Zealand", so any check keyed on the lender name rather than the document content
gets it exactly backwards.
"""

import pytest

from ingestion.jurisdiction import (
    NonAustralianSourceError,
    assert_australian,
    check_jurisdiction,
)

# Condensed from the real ANZ NZ product guide found on an Australian S3 bucket.
ANZ_NZ_GUIDE = """
ANZ Bank New Zealand Limited 02/23 H230225
Visit anz.co.nz for the latest rates. KiwiSaver contributions and Student Loan
repayments are treated as commitments. Auckland City, North $3.75m.
LOW EQUITY PREMIUM (LEP) applies above 80%.
"""

# The genuine ANZ Australia policy matrix.
ANZ_AU_MATRIX = """
Australia and New Zealand Banking Group Limited (ANZ) ABN 11 005 357 522.
Australian credit licence number 234527. A minimum of one financial year's
Individual Tax Return and corresponding Australian Tax Office (ATO) Notice of
Assessment. LMI may apply.
"""


def test_nz_document_is_rejected():
    with pytest.raises(NonAustralianSourceError, match="New Zealand"):
        assert_australian(ANZ_NZ_GUIDE, url="https://example.com/guide.pdf")


def test_anz_australia_is_accepted_despite_new_zealand_in_the_legal_name():
    """The whole trap: ANZ's Australian entity is literally called 'Australia and
    New Zealand Banking Group'. A name-based check fails here."""
    result = assert_australian(ANZ_AU_MATRIX, url="https://www.anz.com/aus/promo/x.pdf")
    assert result["verdict"] == "australian"


def test_nz_content_served_from_a_com_au_domain_is_still_rejected():
    """anz.com.au/newsroom/new-zealand/ -- domain filtering alone is insufficient."""
    with pytest.raises(NonAustralianSourceError):
        assert_australian("Rate changes announced.",
                          url="https://www.anz.com.au/newsroom/new-zealand/2025/11/anz-makes-changes/")


def test_co_nz_url_is_rejected_even_with_australian_sounding_text():
    with pytest.raises(NonAustralianSourceError):
        assert_australian("Home loan serviceability and rental income shading.",
                          url="https://prosperityfinance.co.nz/blog/anz-tightens-servicing")


def test_weak_nz_markers_without_australian_markers_are_rejected():
    assert check_jurisdiction("Our Auckland and Wellington branches, amounts in NZD.")["verdict"] == "new_zealand"


def test_weak_nz_markers_alongside_australian_markers_are_uncertain_not_rejected():
    """An Australian document mentioning a NZ subsidiary must not be auto-rejected --
    it goes to a human instead."""
    text = ("Australian credit licence 233714. The group also operates in Auckland. "
            "HECS and HELP debt are assessed per ATO records.")
    result = check_jurisdiction(text)
    assert result["verdict"] == "uncertain"
    # and must not raise
    assert assert_australian(text)["verdict"] == "uncertain"


def test_a_document_with_no_markers_is_uncertain_not_silently_australian():
    """The failure mode that let the ANZ NZ guides through: absence of evidence
    treated as evidence of Australian-ness."""
    result = check_jurisdiction("Please contact us for a payout figure.")
    assert result["verdict"] == "uncertain"


def test_uncertain_does_not_raise_so_the_guard_stays_usable():
    assert assert_australian("Short page with no markers.")["verdict"] == "uncertain"


def test_the_reason_names_the_markers_that_drove_the_verdict():
    """A bare rejection is unreviewable; the caller must be able to check the call."""
    result = check_jurisdiction(ANZ_NZ_GUIDE)
    assert "kiwisaver" in result["nz_decisive"]
    assert "anz bank new zealand" in result["nz_decisive"]
    assert "decisive NZ markers" in result["reason"]
