"""
Tests for the refinance_offers table, its change-detection reuse, and the
expiry-aware read path.

Network is mocked the same way every other scraper test does it -- these
exercise the real hash-diff/versioning logic, only the fetch is faked.
"""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from db.models import Base, RefinanceOffer, Source
from db.query import get_active_refinance_offers
from ingestion import refinance_offers as offers_module
from ingestion.refinance_offers import (
    check_refinance_offer_source,
    get_open_offer,
    record_refinance_offer,
)
from ingestion.scheduled_pipeline import run_refinance_offer_checks
from ingestion.scrapers import base as scraper_base

PAGE_V1 = b"<html><body>Refinance with us: $3,000 cashback. Offer ends 31 December 2026.</body></html>"
PAGE_V2 = b"<html><body>Refinance with us: $4,000 cashback. Offer ends 31 March 2027.</body></html>"


class FakeResponse:
    def __init__(self, content: bytes = b"", text: str = "", status_code: int = 200):
        self.content = content
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"fake HTTP {self.status_code}")


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(scraper_base, "_robots_cache", {})
    monkeypatch.setattr(scraper_base, "_last_request_at", {})
    monkeypatch.setattr(scraper_base, "MIN_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(offers_module, "DEST_DIR", tmp_path / "sources" / "refinance_offers")


def mock_page(monkeypatch, content: bytes):
    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        if url.endswith("robots.txt"):
            return FakeResponse(text="User-agent: *\nAllow: /\n", status_code=200)
        return FakeResponse(content=content, status_code=200)

    monkeypatch.setattr(scraper_base.httpx, "get", fake_get)


def make_source(session, lender="CBA"):
    source = Source(
        entity_name=lender,
        source_tier="lender_official",
        url="https://example.com/refinance-cashback",
        retrieved_date=date(2026, 9, 1),
        raw_content="stub",
    )
    session.add(source)
    session.flush()
    return source


# --- Change detection (reused mechanism, not a new one) ---


def test_first_check_records_new_source(session, monkeypatch):
    mock_page(monkeypatch, PAGE_V1)
    result = check_refinance_offer_source(session, lender="CBA", url="https://example.com/refinance")

    assert result.status == "new"
    assert result.source is not None
    assert result.source.source_tier == "lender_official"


def test_unchanged_page_is_skipped_without_writing_a_duplicate_source(session, monkeypatch):
    mock_page(monkeypatch, PAGE_V1)
    first = check_refinance_offer_source(session, lender="CBA", url="https://example.com/refinance")
    second = check_refinance_offer_source(session, lender="CBA", url="https://example.com/refinance")

    assert second.status == "unchanged"
    assert second.source.id == first.source.id
    assert len(session.scalars(select(Source)).all()) == 1


def test_changed_page_records_a_new_source_keeping_the_old_one(session, monkeypatch):
    mock_page(monkeypatch, PAGE_V1)
    first = check_refinance_offer_source(session, lender="CBA", url="https://example.com/refinance")

    mock_page(monkeypatch, PAGE_V2)
    second = check_refinance_offer_source(session, lender="CBA", url="https://example.com/refinance")

    assert second.status == "changed"
    assert second.source.id != first.source.id
    assert len(session.scalars(select(Source)).all()) == 2


def test_changed_page_does_not_auto_supersede_existing_offer_rows(session, monkeypatch):
    """
    A marketing page's hash changes for irrelevant reasons too. Auto-closing the
    offer's effective_to on any hash change would write false "offer ended" dates
    into the versioned history -- so it must not happen.
    """
    source = make_source(session)
    offer = record_refinance_offer(
        session, source=source, lender="CBA", offer_name="Refinance cashback",
        cashback_amount_aud=3000.0, observed_on=date(2026, 9, 1),
    )

    mock_page(monkeypatch, PAGE_V2)
    check_refinance_offer_source(session, lender="CBA", url="https://example.com/refinance")

    session.refresh(offer)
    assert offer.effective_to is None  # still open -- not silently ended


def test_fetch_error_is_reported_not_raised(session, monkeypatch):
    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        if url.endswith("robots.txt"):
            return FakeResponse(text="User-agent: *\nAllow: /\n", status_code=200)
        raise RuntimeError("connection refused")

    monkeypatch.setattr(scraper_base.httpx, "get", fake_get)
    result = check_refinance_offer_source(session, lender="CBA", url="https://example.com/refinance")

    assert result.status == "error"
    assert "connection refused" in result.error


def test_pipeline_hook_runs_over_injected_sources(session, monkeypatch):
    mock_page(monkeypatch, PAGE_V1)
    results = run_refinance_offer_checks(
        session, sources=[("CBA", "https://example.com/refinance", None)]
    )
    assert len(results) == 1
    assert results[0].status == "new"


def test_pipeline_registry_is_empty_until_urls_are_researched():
    """Guards against someone assuming the weekly cadence is already doing something."""
    from ingestion.scheduled_pipeline import REFINANCE_OFFER_SOURCES

    assert REFINANCE_OFFER_SOURCES == []


# --- Versioning (4e) ---


def test_recording_an_offer_stores_the_structured_fields(session):
    source = make_source(session)
    offer = record_refinance_offer(
        session,
        source=source,
        lender="CBA",
        offer_name="Refinance cashback",
        cashback_amount_aud=3000.0,
        minimum_loan_amount_aud=250000.0,
        maximum_lvr_pct=80.0,
        eligibility_conditions={"owner_occupier_only": True, "new_to_bank": True},
        clawback_period_months=24,
        clawback_conditions={"repayable_in_full_if": "loan discharged within 24 months"},
        offer_expiry_date=date(2026, 12, 31),
        observed_on=date(2026, 9, 1),
    )

    assert offer.cashback_amount_aud == 3000.0
    assert offer.minimum_loan_amount_aud == 250000.0
    assert offer.maximum_lvr_pct == 80.0
    assert offer.clawback_period_months == 24
    assert offer.clawback_conditions["repayable_in_full_if"].startswith("loan discharged")
    assert offer.offer_expiry_date == date(2026, 12, 31)
    assert offer.effective_to is None
    assert offer.source_id == source.id  # 4g: always traceable


def test_changed_offer_supersedes_rather_than_overwrites(session):
    source = make_source(session)
    first = record_refinance_offer(
        session, source=source, lender="CBA", offer_name="Refinance cashback",
        cashback_amount_aud=3000.0, observed_on=date(2026, 9, 1),
    )
    second = record_refinance_offer(
        session, source=source, lender="CBA", offer_name="Refinance cashback",
        cashback_amount_aud=4000.0, observed_on=date(2026, 10, 1),
    )

    session.refresh(first)
    assert first.effective_to == date(2026, 10, 1)  # closed off, not deleted
    assert second.effective_to is None
    assert len(session.scalars(select(RefinanceOffer)).all()) == 2


def test_re_recording_an_identical_offer_does_not_fragment_history(session):
    source = make_source(session)
    first = record_refinance_offer(
        session, source=source, lender="CBA", offer_name="Refinance cashback",
        cashback_amount_aud=3000.0, observed_on=date(2026, 9, 1),
    )
    again = record_refinance_offer(
        session, source=source, lender="CBA", offer_name="Refinance cashback",
        cashback_amount_aud=3000.0, observed_on=date(2026, 9, 8),
    )

    assert again.id == first.id
    assert len(session.scalars(select(RefinanceOffer)).all()) == 1


def test_point_in_time_query_returns_the_offer_current_on_that_date(session):
    source = make_source(session)
    record_refinance_offer(
        session, source=source, lender="CBA", offer_name="Refinance cashback",
        cashback_amount_aud=3000.0, offer_expiry_date=date(2027, 12, 31), observed_on=date(2026, 9, 1),
    )
    record_refinance_offer(
        session, source=source, lender="CBA", offer_name="Refinance cashback",
        cashback_amount_aud=4000.0, offer_expiry_date=date(2027, 12, 31), observed_on=date(2026, 10, 1),
    )

    back_then = get_active_refinance_offers(session, lender="CBA", as_of=date(2026, 9, 15))
    now = get_active_refinance_offers(session, lender="CBA", as_of=date(2026, 10, 15))

    assert [o.cashback_amount_aud for o in back_then] == [3000.0]
    assert [o.cashback_amount_aud for o in now] == [4000.0]


def test_open_offer_lookup(session):
    source = make_source(session)
    assert get_open_offer(session, "CBA", "Refinance cashback") is None
    recorded = record_refinance_offer(
        session, source=source, lender="CBA", offer_name="Refinance cashback",
        cashback_amount_aud=3000.0, observed_on=date(2026, 9, 1),
    )
    assert get_open_offer(session, "CBA", "Refinance cashback").id == recorded.id


# --- Expiry awareness (the lender's own date, not our versioning window) ---


def test_expired_offer_is_excluded_by_default(session):
    source = make_source(session)
    record_refinance_offer(
        session, source=source, lender="CBA", offer_name="Refinance cashback",
        cashback_amount_aud=3000.0, offer_expiry_date=date(2026, 6, 30), observed_on=date(2026, 1, 1),
    )

    active = get_active_refinance_offers(session, lender="CBA", as_of=date(2026, 9, 1))
    assert active == []


def test_expired_offer_is_returned_flagged_when_explicitly_requested(session):
    source = make_source(session)
    record_refinance_offer(
        session, source=source, lender="CBA", offer_name="Refinance cashback",
        cashback_amount_aud=3000.0, offer_expiry_date=date(2026, 6, 30), observed_on=date(2026, 1, 1),
    )

    stale = get_active_refinance_offers(
        session, lender="CBA", as_of=date(2026, 9, 1), include_expired=True
    )
    assert len(stale) == 1
    assert stale[0].is_expired is True
    assert "EXPIRED OFFER" in stale[0].staleness_warning
    # not suppressed -- the actual figures are still there for the maintenance view
    assert stale[0].cashback_amount_aud == 3000.0


def test_offer_with_no_stated_expiry_is_never_treated_as_expired(session):
    source = make_source(session)
    record_refinance_offer(
        session, source=source, lender="CBA", offer_name="Ongoing cashback",
        cashback_amount_aud=2000.0, offer_expiry_date=None, observed_on=date(2026, 1, 1),
    )

    active = get_active_refinance_offers(session, lender="CBA", as_of=date(2030, 1, 1))
    assert len(active) == 1
    assert active[0].is_expired is False
    assert active[0].staleness_warning is None


def test_offer_carries_full_source_citation(session):
    source = make_source(session)
    record_refinance_offer(
        session, source=source, lender="CBA", offer_name="Refinance cashback",
        cashback_amount_aud=3000.0, offer_expiry_date=date(2027, 1, 1), observed_on=date(2026, 9, 1),
    )

    active = get_active_refinance_offers(session, lender="CBA", as_of=date(2026, 9, 2))
    citation = active[0].source
    assert citation.entity_name == "CBA"
    assert citation.source_tier == "lender_official"
    assert citation.url == "https://example.com/refinance-cashback"


def test_lender_filter_is_optional(session):
    source_cba = make_source(session, "CBA")
    source_nab = make_source(session, "NAB")
    record_refinance_offer(
        session, source=source_cba, lender="CBA", offer_name="A",
        cashback_amount_aud=3000.0, observed_on=date(2026, 9, 1),
    )
    record_refinance_offer(
        session, source=source_nab, lender="NAB", offer_name="B",
        cashback_amount_aud=2000.0, observed_on=date(2026, 9, 1),
    )

    all_offers = get_active_refinance_offers(session, as_of=date(2026, 9, 2))
    assert {o.lender for o in all_offers} == {"CBA", "NAB"}
    assert len(get_active_refinance_offers(session, lender="NAB", as_of=date(2026, 9, 2))) == 1
