"""
Shared scraper utilities, reused by every per-lender scraper module.

Handles: robots.txt compliance, rate limiting, fetching, content hashing, text
extraction (PDF or HTML), saving the raw file under /sources/<lender>/, and
inserting the resulting `sources` row (4g provenance).
"""

import hashlib
import io
import re
import time
from datetime import date
from pathlib import Path
from urllib import robotparser
from urllib.parse import urlparse

import httpx
import pdfplumber
from sqlalchemy.orm import Session

from db.models import Source

USER_AGENT = "AusDebtPolicyDatabaseBot/0.1 (public policy research project)"

# Deliberately conservative: this same code is reused across every lender, so a short
# per-domain delay here keeps *all* scrapers polite by default, not just this one.
MIN_DELAY_SECONDS = 2.0

_robots_cache: dict[str, robotparser.RobotFileParser] = {}
_last_request_at: dict[str, float] = {}


def _domain(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _get_robot_parser(url: str) -> robotparser.RobotFileParser:
    domain = _domain(url)
    if domain not in _robots_cache:
        rp = robotparser.RobotFileParser()
        try:
            resp = httpx.get(f"{domain}/robots.txt", headers={"User-Agent": USER_AGENT}, timeout=10)
            rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
        except httpx.HTTPError:
            rp.parse([])  # can't reach robots.txt -- fail open, matches RobotFileParser default
        _robots_cache[domain] = rp
    return _robots_cache[domain]


def is_allowed(url: str) -> bool:
    return _get_robot_parser(url).can_fetch(USER_AGENT, url)


def _throttle(url: str) -> None:
    domain = urlparse(url).netloc
    last = _last_request_at.get(domain)
    if last is not None:
        remaining = MIN_DELAY_SECONDS - (time.monotonic() - last)
        if remaining > 0:
            time.sleep(remaining)
    _last_request_at[domain] = time.monotonic()


def fetch(url: str) -> bytes:
    """Fetch raw bytes for `url`, respecting robots.txt and the per-domain rate limit."""
    if not is_allowed(url):
        raise PermissionError(f"robots.txt disallows fetching {url}")
    _throttle(url)
    resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def compute_content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def extract_text(content: bytes, url: str) -> str:
    """Extract plain text from a fetched document. Dispatches on file extension."""
    if urlparse(url).path.lower().endswith(".pdf"):
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    text = content.decode("utf-8", errors="replace")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def save_raw_file(content: bytes, dest_path: Path) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(content)


def scrape_documents(
    session: Session,
    *,
    entity_name: str,
    parent_entity: str | None,
    urls: list[str],
    dest_dir: Path,
    source_tier: str = "lender_official",
) -> list[Source]:
    """
    Fetch each URL, save the raw file under dest_dir, extract its text, and insert
    a `sources` row for it. Returns the inserted Source rows (flushed, not committed --
    callers decide when to commit).
    """
    sources = []
    for url in urls:
        content = fetch(url)
        content_hash = compute_content_hash(content)
        filename = Path(urlparse(url).path).name or f"{content_hash[:16]}.bin"
        dest_path = Path(dest_dir) / filename
        save_raw_file(content, dest_path)

        source = Source(
            entity_name=entity_name,
            parent_entity=parent_entity,
            source_tier=source_tier,
            url=url,
            retrieved_date=date.today(),
            raw_file_path=str(dest_path),
            raw_content=extract_text(content, url),
            content_hash=content_hash,
        )
        session.add(source)
        session.flush()
        sources.append(source)
    return sources
