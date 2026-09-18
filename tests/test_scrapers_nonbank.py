"""
Tests for the non-bank lender scrapers. Same mocked-network approach as
tests/test_scraper_cba.py and tests/test_scrapers_big4.py, parametrized across
every lender in nonbank_lenders.ALL_SCRAPERS so each stays independently
testable despite sharing the underlying scrape_documents() plumbing.
"""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from db.models import Base, Source
from ingestion.scrapers import base as scraper_base
from ingestion.scrapers import nonbank_lenders

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
    monkeypatch.setattr(scraper_base, "_robots_cache", {})
    monkeypatch.setattr(scraper_base, "_last_request_at", {})
    monkeypatch.setattr(scraper_base, "MIN_DELAY_SECONDS", 0.01)
    for key, config in nonbank_lenders.LENDERS.items():
        nonbank_lenders.LENDERS[key] = type(config)(
            entity_name=config.entity_name,
            urls=config.urls,
            dest_dir=tmp_path / "sources" / key,
            parent_entity=config.parent_entity,
        )


@pytest.fixture()
def mock_http(monkeypatch):
    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        if url.endswith("robots.txt"):
            return FakeResponse(text="User-agent: *\nAllow: /\n", status_code=200)
        return FakeResponse(content=FIXTURE_BYTES, status_code=200)

    monkeypatch.setattr(scraper_base.httpx, "get", fake_get)


@pytest.mark.parametrize("key", nonbank_lenders.ALL_SCRAPERS.keys())
def test_scraper_inserts_source_records(session, mock_http, key):
    scrape_fn = nonbank_lenders.ALL_SCRAPERS[key]
    config = nonbank_lenders.LENDERS[key]

    inserted = scrape_fn(session)
    session.commit()

    assert len(inserted) == len(config.urls)
    for source, url in zip(inserted, config.urls):
        assert source.entity_name == config.entity_name
        assert source.source_tier == "lender_official"
        assert source.url == url
        assert source.retrieved_date == date.today()
        assert source.content_hash == scraper_base.compute_content_hash(FIXTURE_BYTES)

        raw_path = Path(source.raw_file_path)
        assert raw_path.exists()
        assert raw_path.read_bytes() == FIXTURE_BYTES

    persisted = session.scalars(select(Source).where(Source.entity_name == config.entity_name)).all()
    assert len(persisted) == len(config.urls)


@pytest.mark.parametrize("key", nonbank_lenders.ALL_SCRAPERS.keys())
def test_scraper_respects_robots_disallow(session, monkeypatch, key):
    scrape_fn = nonbank_lenders.ALL_SCRAPERS[key]

    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        if url.endswith("robots.txt"):
            return FakeResponse(text="User-agent: *\nDisallow: /\n", status_code=200)
        return FakeResponse(content=FIXTURE_BYTES, status_code=200)

    monkeypatch.setattr(scraper_base.httpx, "get", fake_get)

    with pytest.raises(PermissionError):
        scrape_fn(session)


def test_afterpay_has_parent_entity_recorded(session, mock_http):
    inserted = nonbank_lenders.scrape_afterpay(session)
    assert all(s.parent_entity == "Block, Inc." for s in inserted)


@pytest.mark.parametrize("key", ["resimac", "latitude"])
def test_blocked_lenders_have_no_scraper(key):
    assert key not in nonbank_lenders.ALL_SCRAPERS
    assert key not in nonbank_lenders.LENDERS
