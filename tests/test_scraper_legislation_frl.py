"""
Tests for the Federal Register of Legislation scraper. Mocks httpx.get rather
than hitting the live Register, same rationale as the lender scraper tests --
plus this site is a client-rendered SPA for anything except the exact dated
document URLs, so a "live" test here would be especially fragile.
"""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from db.models import Base, Source
from ingestion.scrapers import base as scraper_base
from ingestion.scrapers import legislation_frl

FIXTURE_PDF = Path(__file__).parent / "fixtures" / "frl_sample.pdf"
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
    monkeypatch.setattr(legislation_frl, "DEST_DIR", tmp_path / "sources" / "legislation")


@pytest.fixture()
def mock_http(monkeypatch):
    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        if url.endswith("robots.txt"):
            return FakeResponse(text="User-agent: *\nAllow: /\n", status_code=200)
        return FakeResponse(content=FIXTURE_BYTES, status_code=200)

    monkeypatch.setattr(scraper_base.httpx, "get", fake_get)


def test_parse_compilation_metadata_extracts_real_fields():
    from ingestion.scrapers.base import extract_text

    extracted = extract_text(FIXTURE_BYTES, "https://example.com/fake.pdf")
    metadata = legislation_frl.parse_compilation_metadata(extracted)

    assert metadata["compilation_no"] == "101"
    assert metadata["compilation_date"] == "10 June 2025"
    assert metadata["includes_amendments"] == "Act No. 118, 2024"
    assert metadata["authorised_version_id"] == "C2025C00341"
    assert metadata["authorised_version_registered"] == "10/06/2025"


def test_scrape_inserts_one_source_per_target(session, mock_http):
    inserted = legislation_frl.scrape_legislation_frl(session)
    session.commit()

    assert len(inserted) == len(legislation_frl.TARGETS)
    for source, target in zip(inserted, legislation_frl.TARGETS):
        assert source.entity_name == target.act_name
        assert source.source_tier == "statute"
        assert source.url == legislation_frl._build_pdf_url(target)
        assert source.content_hash == scraper_base.compute_content_hash(FIXTURE_BYTES)

        raw_path = Path(source.raw_file_path)
        assert raw_path.exists()
        assert raw_path.read_bytes() == FIXTURE_BYTES


def test_source_tier_is_statute_not_lender_official(session, mock_http):
    inserted = legislation_frl.scrape_legislation_frl(session)
    assert all(s.source_tier == "statute" for s in inserted)


def test_raw_content_includes_deterministic_metadata_block_and_licence_attribution(session, mock_http):
    inserted = legislation_frl.scrape_legislation_frl(session)

    for source in inserted:
        assert "[FRL COMPILATION METADATA" in source.raw_content
        assert "Compilation No.: 101" in source.raw_content
        assert "Compilation date (commencement of this compilation): 10 June 2025" in source.raw_content
        assert "Status: in_force" in source.raw_content
        assert "Sourced from the Federal Register of Legislation at" in source.raw_content
        assert "https://www.legislation.gov.au" in source.raw_content
        # the actual document text still follows the metadata block
        assert "Family Law Act 1975" in source.raw_content


def test_persisted_sources_queryable_by_entity_name(session, mock_http):
    legislation_frl.scrape_legislation_frl(session)
    session.commit()

    persisted = session.scalars(select(Source).where(Source.entity_name == "Corporations Act 2001")).all()
    assert len(persisted) == 1
    assert persisted[0].source_tier == "statute"


def test_scraper_respects_robots_disallow(session, monkeypatch):
    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        if url.endswith("robots.txt"):
            return FakeResponse(text="User-agent: *\nDisallow: /\n", status_code=200)
        return FakeResponse(content=FIXTURE_BYTES, status_code=200)

    monkeypatch.setattr(scraper_base.httpx, "get", fake_get)

    with pytest.raises(PermissionError):
        legislation_frl.scrape_legislation_frl(session)
