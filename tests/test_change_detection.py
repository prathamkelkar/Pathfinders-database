"""
Tests for ingestion/change_detection.py (lender-generic path) and
ingestion/scrapers/legislation_frl.py's check_legislation_targets()
(legislation-specific path).
"""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from db.models import Base, CandidateRule, Source
from ingestion import change_detection
from ingestion.scrapers import base as scraper_base
from ingestion.scrapers import legislation_frl

FIXTURE_PDF = Path(__file__).parent / "fixtures" / "cba_sample.pdf"
FIXTURE_BYTES = FIXTURE_PDF.read_bytes()
FIXTURE_BYTES_V2 = FIXTURE_BYTES.replace(b"HECS", b"XXXX")  # same shape, different hash

FRL_FIXTURE_PDF = Path(__file__).parent / "fixtures" / "frl_sample.pdf"
FRL_FIXTURE_BYTES = FRL_FIXTURE_PDF.read_bytes()


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
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setattr(scraper_base, "_robots_cache", {})
    monkeypatch.setattr(scraper_base, "_last_request_at", {})
    monkeypatch.setattr(scraper_base, "MIN_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(legislation_frl, "DEST_DIR", tmp_path / "sources" / "legislation")
    monkeypatch.setattr(legislation_frl, "REMINDER_STATE_FILE", tmp_path / "reminder_state.json")


def _mock_http(monkeypatch, content: bytes):
    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        if url.endswith("robots.txt"):
            return FakeResponse(text="User-agent: *\nAllow: /\n", status_code=200)
        return FakeResponse(content=content, status_code=200)

    monkeypatch.setattr(scraper_base.httpx, "get", fake_get)


@pytest.fixture()
def fake_extract_candidates(monkeypatch):
    calls = []

    def fake(session, source, **kwargs):
        calls.append(source.id)
        candidate = CandidateRule(
            lender=source.entity_name,
            debt_type="home_loan",
            conditions={},
            effect={"stub": True},
            source_tier=source.source_tier,
            effective_from=date.today(),
            confidence="official_document",
            conflicting_sources=False,
            extraction_prompt_version="1",
            source_id=source.id,
        )
        session.add(candidate)
        session.flush()
        return [candidate]

    monkeypatch.setattr(change_detection, "extract_candidates", fake)
    monkeypatch.setattr("ingestion.extraction.extract_rules.extract_candidates", fake)
    return calls


DEST = Path("/tmp/change_detection_test_dest")


def test_first_check_is_new_and_triggers_extraction(session, monkeypatch, fake_extract_candidates, tmp_path):
    _mock_http(monkeypatch, FIXTURE_BYTES)
    result = change_detection.check_lender_source(
        session, entity_name="CBA", parent_entity=None, url="https://example.com/doc.pdf", dest_dir=tmp_path / "cba"
    )

    assert result.status == "new"
    assert result.source is not None
    assert result.candidates_created == 1
    assert fake_extract_candidates == [result.source.id]
    assert session.scalars(select(Source)).all().__len__() == 1


def test_second_check_with_same_content_is_unchanged_and_skips_extraction(
    session, monkeypatch, fake_extract_candidates, tmp_path
):
    _mock_http(monkeypatch, FIXTURE_BYTES)
    first = change_detection.check_lender_source(
        session, entity_name="CBA", parent_entity=None, url="https://example.com/doc.pdf", dest_dir=tmp_path / "cba"
    )
    fake_extract_candidates.clear()

    second = change_detection.check_lender_source(
        session, entity_name="CBA", parent_entity=None, url="https://example.com/doc.pdf", dest_dir=tmp_path / "cba"
    )

    assert second.status == "unchanged"
    assert second.source.id == first.source.id
    assert second.candidates_created == 0
    assert fake_extract_candidates == []  # extraction never called
    assert session.scalars(select(Source)).all().__len__() == 1  # no duplicate row


def test_third_check_with_changed_content_is_changed_and_triggers_extraction(
    session, monkeypatch, fake_extract_candidates, tmp_path
):
    _mock_http(monkeypatch, FIXTURE_BYTES)
    first = change_detection.check_lender_source(
        session, entity_name="CBA", parent_entity=None, url="https://example.com/doc.pdf", dest_dir=tmp_path / "cba"
    )
    fake_extract_candidates.clear()

    _mock_http(monkeypatch, FIXTURE_BYTES_V2)
    second = change_detection.check_lender_source(
        session, entity_name="CBA", parent_entity=None, url="https://example.com/doc.pdf", dest_dir=tmp_path / "cba"
    )

    assert second.status == "changed"
    assert second.source.id != first.source.id
    assert second.candidates_created == 1
    # old source row still exists -- versioned, not overwritten
    assert session.scalars(select(Source)).all().__len__() == 2


def test_fetch_error_is_reported_not_raised(session, monkeypatch, tmp_path):
    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        if url.endswith("robots.txt"):
            return FakeResponse(text="User-agent: *\nAllow: /\n", status_code=200)
        raise RuntimeError("connection refused")

    monkeypatch.setattr(scraper_base.httpx, "get", fake_get)

    result = change_detection.check_lender_source(
        session, entity_name="CBA", parent_entity=None, url="https://example.com/doc.pdf", dest_dir=tmp_path / "cba"
    )

    assert result.status == "error"
    assert "connection refused" in result.error
    assert session.scalars(select(Source)).all() == []


def test_extraction_failure_still_persists_the_source(session, monkeypatch, tmp_path):
    _mock_http(monkeypatch, FIXTURE_BYTES)

    def failing_extract(session, source, **kwargs):
        raise ValueError("malformed JSON from LLM")

    monkeypatch.setattr(change_detection, "extract_candidates", failing_extract)

    result = change_detection.check_lender_source(
        session, entity_name="CBA", parent_entity=None, url="https://example.com/doc.pdf", dest_dir=tmp_path / "cba"
    )

    assert result.status == "new"
    assert result.candidates_created == 0
    assert result.source is not None
    assert session.scalars(select(Source)).all().__len__() == 1


def test_extraction_status_ok_when_candidates_created(session, monkeypatch, fake_extract_candidates, tmp_path):
    _mock_http(monkeypatch, FIXTURE_BYTES)
    result = change_detection.check_lender_source(
        session, entity_name="CBA", parent_entity=None, url="https://example.com/doc.pdf", dest_dir=tmp_path / "cba"
    )
    assert result.extraction_status == "ok"


def test_extraction_status_distinguishes_failed_after_retries_from_ok(session, monkeypatch, tmp_path):
    from ingestion.extraction.extract_rules import ExtractionFailedError

    _mock_http(monkeypatch, FIXTURE_BYTES)

    def raises_failed(session, source, **kwargs):
        raise ExtractionFailedError("model never returned usable JSON")

    monkeypatch.setattr(change_detection, "extract_candidates", raises_failed)

    result = change_detection.check_lender_source(
        session, entity_name="CBA", parent_entity=None, url="https://example.com/doc.pdf", dest_dir=tmp_path / "cba"
    )

    assert result.extraction_status == "extraction_failed_after_retries"
    assert result.candidates_created == 0
    # the source itself is still persisted -- only extraction failed, not the fetch
    assert result.source is not None


def test_unchanged_and_changed_log_messages_are_distinct(session, monkeypatch, fake_extract_candidates, tmp_path, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="ingestion.change_detection")

    _mock_http(monkeypatch, FIXTURE_BYTES)
    change_detection.check_lender_source(
        session, entity_name="CBA", parent_entity=None, url="https://example.com/doc.pdf", dest_dir=tmp_path / "cba"
    )
    change_detection.check_lender_source(
        session, entity_name="CBA", parent_entity=None, url="https://example.com/doc.pdf", dest_dir=tmp_path / "cba"
    )

    messages = [r.message for r in caplog.records]
    assert any("NEW" in m for m in messages)
    assert any("UNCHANGED" in m for m in messages)
    assert not any("NEW" in m and "UNCHANGED" in m for m in messages)  # never conflated in one line


# --- Legislation-specific path ---


def test_legislation_check_unchanged_across_targets(session, monkeypatch, fake_extract_candidates):
    _mock_http(monkeypatch, FRL_FIXTURE_BYTES)
    first = legislation_frl.check_legislation_targets(session)
    fake_extract_candidates.clear()

    second = legislation_frl.check_legislation_targets(session)

    assert all(r.status == "new" for r in first)
    assert all(r.status == "unchanged" for r in second)
    assert fake_extract_candidates == []
    assert session.scalars(select(Source)).all().__len__() == len(legislation_frl.TARGETS)


def test_legislation_check_new_content_includes_metadata_block(session, monkeypatch, fake_extract_candidates):
    _mock_http(monkeypatch, FRL_FIXTURE_BYTES)
    results = legislation_frl.check_legislation_targets(session)

    for r in results:
        assert r.status == "new"
        assert "[FRL COMPILATION METADATA" in r.source.raw_content
        assert r.source.source_tier == "statute"


def test_legislation_reminder_emitted_once_then_suppressed(session, monkeypatch, fake_extract_candidates, caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="ingestion.change_detection")

    _mock_http(monkeypatch, FRL_FIXTURE_BYTES)
    legislation_frl.check_legislation_targets(session)
    first_reminders = [r for r in caplog.records if "QUARTERLY REMINDER" in r.message]
    assert len(first_reminders) == 1

    caplog.clear()
    legislation_frl.check_legislation_targets(session)
    second_reminders = [r for r in caplog.records if "QUARTERLY REMINDER" in r.message]
    assert len(second_reminders) == 0  # suppressed within the interval
