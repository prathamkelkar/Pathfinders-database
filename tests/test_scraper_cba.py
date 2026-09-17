"""
Test for the CBA scraper (ingestion/scrapers/cba.py).

Design choice: this test does NOT hit the live CBA site. It monkeypatches
`httpx.get` inside ingestion.scrapers.base to return a small saved sample PDF
fixture (tests/fixtures/cba_sample.pdf) instead of the real document.

Why: the scraper is explicitly designed to be reused, unmodified, across every
lender in the project. A test suite that hits a live bank server on every run
would itself become the "hammering" pattern the rate limiter exists to avoid --
pytest can run dozens of times an hour during development/CI, whereas real
scraping should happen on a schedule. Mocking at the httpx.get layer (rather
than mocking scrape_documents() itself) still exercises the real robots.txt
check, rate-limit bookkeeping, hashing, text extraction, file-saving, and DB
insertion logic -- only the actual network call is faked.
"""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from db.models import Base, Source
from ingestion.scrapers import base as scraper_base
from ingestion.scrapers import cba

FIXTURE_PDF = Path(__file__).parent / "fixtures" / "cba_sample.pdf"
FIXTURE_BYTES = FIXTURE_PDF.read_bytes()


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
def _isolate_scraper_state(monkeypatch, tmp_path):
    # module-level robots/rate-limit caches shouldn't leak between tests
    monkeypatch.setattr(scraper_base, "_robots_cache", {})
    monkeypatch.setattr(scraper_base, "_last_request_at", {})
    # don't write fixture output into the real /sources/cba/ directory
    monkeypatch.setattr(cba, "DEST_DIR", tmp_path / "sources" / "cba")
    # keep the test fast -- the delay logic itself is exercised, just not slowly
    monkeypatch.setattr(scraper_base, "MIN_DELAY_SECONDS", 0.01)


@pytest.fixture()
def mock_http(monkeypatch):
    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        if url.endswith("robots.txt"):
            return FakeResponse(text="User-agent: *\nAllow: /\n", status_code=200)
        return FakeResponse(content=FIXTURE_BYTES, status_code=200)

    monkeypatch.setattr(scraper_base.httpx, "get", fake_get)


def test_scrape_cba_inserts_source_records(session, mock_http):
    inserted = cba.scrape_cba(session)
    session.commit()

    assert len(inserted) == len(cba.DOCUMENT_URLS)

    for source, url in zip(inserted, cba.DOCUMENT_URLS):
        assert source.entity_name == "CBA"
        assert source.source_tier == "lender_official"
        assert source.url == url
        assert source.retrieved_date == date.today()
        assert source.content_hash == scraper_base.compute_content_hash(FIXTURE_BYTES)
        assert "HECS HELP serviceability test" in source.raw_content

        raw_path = Path(source.raw_file_path)
        assert raw_path.exists()
        assert raw_path.read_bytes() == FIXTURE_BYTES

    persisted = session.scalars(select(Source).where(Source.entity_name == "CBA")).all()
    assert len(persisted) == len(cba.DOCUMENT_URLS)


def test_robots_disallow_blocks_fetch(session, monkeypatch):
    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        if url.endswith("robots.txt"):
            return FakeResponse(text="User-agent: *\nDisallow: /\n", status_code=200)
        return FakeResponse(content=FIXTURE_BYTES, status_code=200)

    monkeypatch.setattr(scraper_base.httpx, "get", fake_get)

    with pytest.raises(PermissionError):
        cba.scrape_cba(session)
