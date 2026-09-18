from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from db.models import Base, Source
from ingestion.scrapers import apra_apg223
from ingestion.scrapers import base as scraper_base

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
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(scraper_base, "_robots_cache", {})
    monkeypatch.setattr(scraper_base, "_last_request_at", {})
    monkeypatch.setattr(scraper_base, "MIN_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(apra_apg223, "DEST_DIR", tmp_path / "sources" / "apra")


@pytest.fixture()
def mock_http(monkeypatch):
    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        if url.endswith("robots.txt"):
            return FakeResponse(text="User-agent: *\nAllow: /\n", status_code=200)
        return FakeResponse(content=FIXTURE_BYTES, status_code=200)

    monkeypatch.setattr(scraper_base.httpx, "get", fake_get)


def test_scrape_inserts_both_documents(session, mock_http):
    inserted = apra_apg223.scrape_apra_apg223(session)
    session.commit()

    assert len(inserted) == 2
    for source, doc in zip(inserted, apra_apg223.DOCUMENTS):
        assert source.entity_name == "APRA"
        assert source.source_tier == "regulator_guidance"
        assert source.url == doc.url
        assert "[APRA DOCUMENT METADATA]" in source.raw_content
        assert doc.title in source.raw_content
        assert "CC BY 4.0" in source.raw_content

        raw_path = Path(source.raw_file_path)
        assert raw_path.exists()

    persisted = session.scalars(select(Source).where(Source.entity_name == "APRA")).all()
    assert len(persisted) == 2


def test_source_tier_is_regulator_guidance(session, mock_http):
    inserted = apra_apg223.scrape_apra_apg223(session)
    assert all(s.source_tier == "regulator_guidance" for s in inserted)
