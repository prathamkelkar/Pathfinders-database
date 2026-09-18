"""
Tests for the Westpac, NAB, and ANZ scrapers -- same approach as
tests/test_scraper_cba.py: mock httpx.get rather than hitting the live sites,
for the same reasons (this code is reused per-lender; a test suite that hits
live bank servers on every run would itself be the "hammering" pattern the
rate limiter exists to prevent).
"""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from db.models import Base, Source
from ingestion.scrapers import base as scraper_base
from ingestion.scrapers import anz, nab, westpac

FIXTURE_PDF = Path(__file__).parent / "fixtures" / "cba_sample.pdf"
FIXTURE_BYTES = FIXTURE_PDF.read_bytes()

MODULES = {
    "westpac": (westpac, westpac.scrape_westpac, "Westpac"),
    "nab": (nab, nab.scrape_nab, "NAB"),
    "anz": (anz, anz.scrape_anz, "ANZ"),
}


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
    monkeypatch.setattr(scraper_base, "_robots_cache", {})
    monkeypatch.setattr(scraper_base, "_last_request_at", {})
    monkeypatch.setattr(scraper_base, "MIN_DELAY_SECONDS", 0.01)
    for module, _, _ in MODULES.values():
        monkeypatch.setattr(module, "DEST_DIR", tmp_path / "sources" / module.ENTITY_NAME.lower())


@pytest.fixture()
def mock_http(monkeypatch):
    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        if url.endswith("robots.txt"):
            return FakeResponse(text="User-agent: *\nAllow: /\n", status_code=200)
        return FakeResponse(content=FIXTURE_BYTES, status_code=200)

    monkeypatch.setattr(scraper_base.httpx, "get", fake_get)


@pytest.mark.parametrize("key", MODULES.keys())
def test_scraper_inserts_source_records(session, mock_http, key):
    module, scrape_func, entity_name = MODULES[key]

    inserted = scrape_func(session)
    session.commit()

    assert len(inserted) == len(module.DOCUMENT_URLS)
    for source, url in zip(inserted, module.DOCUMENT_URLS):
        assert source.entity_name == entity_name
        assert source.source_tier == "lender_official"
        assert source.url == url
        assert source.retrieved_date == date.today()
        assert source.content_hash == scraper_base.compute_content_hash(FIXTURE_BYTES)
        assert "HECS HELP serviceability test" in source.raw_content

        raw_path = Path(source.raw_file_path)
        assert raw_path.exists()
        assert raw_path.read_bytes() == FIXTURE_BYTES

    persisted = session.scalars(select(Source).where(Source.entity_name == entity_name)).all()
    assert len(persisted) == len(module.DOCUMENT_URLS)


@pytest.mark.parametrize("key", MODULES.keys())
def test_scraper_respects_robots_disallow(session, monkeypatch, key):
    _, scrape_func, _ = MODULES[key]

    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        if url.endswith("robots.txt"):
            return FakeResponse(text="User-agent: *\nDisallow: /\n", status_code=200)
        return FakeResponse(content=FIXTURE_BYTES, status_code=200)

    monkeypatch.setattr(scraper_base.httpx, "get", fake_get)

    with pytest.raises(PermissionError):
        scrape_func(session)
