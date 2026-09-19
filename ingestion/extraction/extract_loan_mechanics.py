"""
Targeted re-extraction pass (extraction_prompt_version "2") over sources we have
ALREADY scraped, hunting specifically for the loan-mechanics fields added in the
refinancing/switching schema work: break costs, early exit fees, and the
discharge process/timeline.

Why a second pass over the same documents rather than new scraping: prompt
version "1" asked for "every explicit rule" in general. That reliably surfaced
headline terms (rates, redraw, offset, default) but had no reason to dig for
break-cost formulas or discharge timelines specifically, and on a 250k-character
T&Cs booklet a general prompt simply doesn't have room to be thorough about
everything. The data is very likely already sitting in the documents we hold.
4i's prompt-versioning exists precisely so this kind of re-extraction is
traceable: every candidate this produces is tagged "2", so it's always clear
which pass found what.

TWO-STAGE DESIGN (and why it isn't just "send the document to the LLM again"):

Stage 1 is a deterministic keyword scan, no LLM. It answers the question the
task actually cares about -- is this information GENUINELY ABSENT from the
document, or was it merely never extracted? An LLM returning an empty array
can't distinguish those two: it might have missed something, or the topic might
genuinely not be there. A vocabulary scan can, and it can't hallucinate. So each
(source, topic) pair lands in one of three states:

  - "absent"            : the document contains no vocabulary for this topic at
                          all. Genuinely not in this source -> a real reason to
                          go scrape something else.
  - "found"             : vocabulary present AND the targeted pass extracted
                          rules from it.
  - "present_not_extracted" : vocabulary present but the pass extracted nothing.
                          NOT the same as absent -- this means the text discusses
                          the topic but the model couldn't turn it into a
                          structured rule. Needs a human look, not a re-scrape.

Stage 2 sends only the EXCERPTS around those keyword hits, not the whole
document. That is what makes this pass affordable and reliable: the full corpus
is ~1M characters, and the earlier full-document runs took 400-800s per large
document with roughly a third hitting malformed-JSON failures. Narrowing a 250k
booklet to a few thousand characters of relevant context is faster, cheaper, and
markedly more accurate, because the model isn't hunting a needle in a haystack.
The stored candidate still links to the full source record, so provenance is
unchanged.
"""

import logging
import re
from dataclasses import dataclass, field
from functools import partial

from sqlalchemy.orm import Session

from db.models import CandidateRule, Source
from ingestion.extraction.extract_rules import ExtractionFailedError, call_llm, extract_candidates

logger = logging.getLogger("ingestion.extraction")

LOAN_MECHANICS_PROMPT_VERSION = "2"

# Excerpt window either side of a keyword hit. Wide enough to carry a whole
# clause and its surrounding sentence, narrow enough that a document with many
# hits doesn't collapse back into "send the whole thing".
EXCERPT_RADIUS_CHARS = 1200
MAX_EXCERPT_CHARS_PER_TOPIC = 24000

# Vocabulary per topic. Deliberately broad -- a false positive here just means we
# spend one LLM call and get an honest "present_not_extracted"; a false negative
# would wrongly declare the information genuinely absent and send us scraping for
# something we already had. Erring toward over-inclusion is the safer failure.
TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "break_cost": (
        "break cost", "break fee", "break the fixed", "breaking your fixed",
        "early repayment adjustment", "early repayment fee", "early repayment charge",
        "early termination fee", "economic cost", "prepayment", "pay out early",
        "repay early", "early exit", "exit fee", "deferred establishment fee",
        "administrative fee", "swap rate",
    ),
    "discharge": (
        "discharge", "release of mortgage", "releasing the mortgage", "payout figure",
        "pay out figure", "settlement figure", "final payout", "close your loan",
        "closing your loan", "loan is repaid in full", "title deed", "certificate of title",
        "security release",
    ),
}

SYSTEM_PROMPT_V2 = """detailed thinking off

You are a precise information-extraction system for an Australian debt-policy
database. You are given EXCERPTS from one lender's own terms and conditions,
selected because they mention break costs, exit fees, or the loan discharge
process. Extract every EXPLICIT, structured rule they state about those topics.

Return ONLY a JSON array (no prose, no markdown code fences). Each element is an
object with exactly these fields:
- "policy_area": either "break_cost" (any cost, fee or adjustment charged for
  repaying/exiting/breaking a loan or fixed term early) or "discharge" (the
  process, requirements, fees or timeline for discharging a loan and releasing
  the security once it is repaid). Use exactly one of those two strings.
- "debt_type": string category, e.g. "home_loan", "personal_loan", "credit_card",
  "BNPL" -- null if the excerpts do not make clear which product this applies to
- "conditions": an object describing WHEN the rule applies, using whatever keys
  best fit the specific thresholds the text states, e.g.
  {"loan_type": "Fixed Rate", "action": "full prepayment"} -- {} if unconditional
- "effect": an object describing WHAT happens. For break costs this is the
  important one -- these are usually a FORMULA, not a flat number:
    * if the text describes how the cost is CALCULATED, put that description in
      "calculation_basis" as a string, e.g. {"early_repayment_adjustment_charged":
      true, "calculation_basis": "difference between the wholesale swap rate at
      fixing and at prepayment for the balance of the fixed period"}
    * only use a numeric key like "fee_amount_aud" when the text states an actual
      fixed dollar amount
    * for discharge, useful keys include "process_description",
      "typical_timeline_days", "fee_aud"
- "effective_from": ISO date (YYYY-MM-DD) ONLY if explicitly stated, else null
- "effective_to": ISO date ONLY if explicitly stated, else null

CRITICAL RULES -- follow these exactly:
1. Extract ONLY what is explicitly stated in the given excerpts. Do NOT use
   outside knowledge about this lender, industry norms, or common practice.
2. If a value is not stated, it MUST be null (or {} for conditions/effect).
   NEVER guess, infer, estimate, or invent a plausible-sounding number. In
   particular, never convert a described formula into a made-up dollar figure.
3. If the excerpts contain no extractable rule about break costs, exit fees or
   discharge, return an empty JSON array: []
4. Ignore anything in the excerpts unrelated to those topics -- do not pad the
   output with general rules that happen to appear nearby.

The following shows the REQUIRED OUTPUT SHAPE ONLY -- it is unrelated to the
document you will be given and must not influence what you extract:
[{"policy_area": "break_cost", "debt_type": "home_loan", "conditions": {"loan_type": "Fixed Rate"}, "effect": {"calculation_basis": "example only"}, "effective_from": null, "effective_to": null}]
"""


@dataclass
class TopicResult:
    topic: str
    coverage: str  # "absent" | "found" | "present_not_extracted" | "extraction_failed"
    keyword_hits: int = 0
    matched_keywords: list[str] = field(default_factory=list)
    excerpt_chars: int = 0
    candidates_created: int = 0
    error: str | None = None


@dataclass
class SourceResult:
    source_id: int
    lender: str
    url: str | None
    topics: dict[str, TopicResult] = field(default_factory=dict)


def find_keyword_hits(text: str, keywords: tuple[str, ...]) -> list[tuple[int, str]]:
    """Case-insensitive positions of every keyword occurrence, in document order."""
    lowered = text.lower()
    hits: list[tuple[int, str]] = []
    for kw in keywords:
        for match in re.finditer(re.escape(kw.lower()), lowered):
            hits.append((match.start(), kw))
    return sorted(hits)


def build_excerpts(text: str, hits: list[tuple[int, str]], radius: int = EXCERPT_RADIUS_CHARS,
                   max_chars: int = MAX_EXCERPT_CHARS_PER_TOPIC) -> str:
    """
    Merge overlapping windows around each hit into contiguous excerpts, capped so
    a keyword-dense document can't quietly turn back into a full-document send.
    """
    if not hits:
        return ""

    windows: list[list[int]] = []
    for position, _ in hits:
        start, end = max(0, position - radius), min(len(text), position + radius)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])

    chunks, total = [], 0
    for start, end in windows:
        chunk = text[start:end]
        if total + len(chunk) > max_chars:
            chunks.append(chunk[: max(0, max_chars - total)])
            break
        chunks.append(chunk)
        total += len(chunk)

    return "\n\n[...]\n\n".join(c for c in chunks if c)


def extract_loan_mechanics_from_source(
    session: Session,
    source: Source,
    *,
    llm_call=None,
    topics: dict[str, tuple[str, ...]] | None = None,
) -> SourceResult:
    """
    Run the targeted pass over one already-scraped source. Makes at most one LLM
    call per topic that actually has vocabulary present -- topics with no
    vocabulary are resolved as genuinely "absent" without any call at all.
    """
    topics = topics if topics is not None else TOPIC_KEYWORDS
    llm_call = llm_call or partial(call_llm, system_prompt=SYSTEM_PROMPT_V2)

    result = SourceResult(source_id=source.id, lender=source.entity_name, url=source.url)
    text = source.raw_content or ""

    for topic, keywords in topics.items():
        hits = find_keyword_hits(text, keywords)
        if not hits:
            logger.info(
                "ABSENT        entity=%s source_id=%s topic=%s -- no vocabulary for this topic in the document",
                source.entity_name, source.id, topic,
            )
            result.topics[topic] = TopicResult(topic=topic, coverage="absent")
            continue

        excerpts = build_excerpts(text, hits)
        matched = sorted({kw for _, kw in hits})

        try:
            created = extract_candidates(
                session,
                source,
                llm_call=llm_call,
                prompt_version=LOAN_MECHANICS_PROMPT_VERSION,
                text_override=excerpts,
                default_policy_area=topic,
            )
        except ExtractionFailedError as e:
            logger.error(
                "EXTRACTION_FAILED_AFTER_RETRIES entity=%s source_id=%s topic=%s",
                source.entity_name, source.id, topic,
            )
            result.topics[topic] = TopicResult(
                topic=topic, coverage="extraction_failed", keyword_hits=len(hits),
                matched_keywords=matched, excerpt_chars=len(excerpts), error=str(e),
            )
            continue

        # Commit per topic, not at the end of the run. extract_candidates() only
        # flushes -- it leaves the commit to its caller -- and this pass makes slow
        # LLM calls across ~20 documents, so a run that dies partway through must
        # keep the work it already paid for. An earlier version of this runner
        # committed nowhere and lost several hours of completed extraction when the
        # process was killed.
        session.commit()

        coverage = "found" if created else "present_not_extracted"
        logger.info(
            "%-13s entity=%s source_id=%s topic=%s hits=%d excerpt_chars=%d -> %d candidate(s)",
            coverage.upper(), source.entity_name, source.id, topic, len(hits), len(excerpts), len(created),
        )
        result.topics[topic] = TopicResult(
            topic=topic, coverage=coverage, keyword_hits=len(hits), matched_keywords=matched,
            excerpt_chars=len(excerpts), candidates_created=len(created),
        )

    return result
