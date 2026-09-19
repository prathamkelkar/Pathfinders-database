"""
Vocabulary-discovery pass: finds the phrasings TOPIC_KEYWORDS is missing.

Why this exists. The break-cost/discharge scan decides "is this topic genuinely
absent from the document, or did we merely fail to look for it?" by matching a
hand-written keyword list. That list is the single point of failure: a phrasing
nobody thought of produces a CONFIDENT false negative -- the report says the
information isn't there, and someone goes scraping for a document we already
hold, or records a real policy as non-existent.

That is not hypothetical. Afterpay's Pay Monthly Product Terms scanned as
"absent" for break_cost across 181,000 characters, because clause 5.6 is headed
"Early payments" and the vocabulary only had "early repayment fee", "repay
early", "prepayment" and similar. The provision was there the whole time.

Adding keywords by imagination cannot fix that -- it only patches the phrasings
we already thought of. So this pass inverts the direction: it hands a suspicious
document to the model and asks what vocabulary the document ACTUALLY uses,
rather than asking whether our vocabulary appears in it.

Every returned phrase is verified to occur verbatim in the source text before it
is reported. A model asked to quote will sometimes paraphrase, and a
hallucinated phrase promoted into TOPIC_KEYWORDS would be permanent dead weight
that quietly never matches anything.
"""

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from db.models import Source
from ingestion.extraction.extract_loan_mechanics import TOPIC_KEYWORDS, find_keyword_hits
from ingestion.extraction.extract_rules import _get_client, NVIDIA_MODEL, REQUEST_TIMEOUT_SECONDS

logger = logging.getLogger("ingestion.extraction")

# An "absent" verdict on a document this large is the high-risk case worth paying
# to check: large documents are the ones that DO discuss loan mechanics, so
# silence from the scanner is more likely a vocabulary gap than a real gap.
SUSPICIOUS_ABSENT_CHARS = 20000

# Cap what we send. Well above the largest document in the corpus (~67k chars),
# so nothing is currently truncated, but present so one unexpectedly huge file
# can't blow up a run.
MAX_DISCOVERY_CHARS = 120000

DISCOVERY_PROMPT = """detailed thinking off

You are auditing an Australian lender's document for a policy database. Your job
is NOT to extract rules. It is to report the exact WORDING the document uses when
it talks about two topics:

- "break_cost": repaying, exiting, paying out, closing or breaking a loan, a fixed
  term, or a credit contract EARLY -- including any fee, charge, adjustment or
  cost for doing so, AND including statements that no such fee applies, AND
  including provisions that merely permit early payment without mentioning a fee.
- "discharge": the process, requirements, fees or timing for discharging a loan
  and releasing any security once it is repaid -- payout figures, release of
  mortgage, certificates of title, security releases.

Return ONLY a JSON array (no prose, no markdown fences). Each element:
- "topic": exactly "break_cost" or "discharge"
- "phrase": a SHORT distinctive phrase, 2 to 6 words, copied EXACTLY character for
  character from the document, that a keyword search could use to find this
  passage. Lowercase it.
- "quote": a longer verbatim sentence from the document containing that phrase,
  copied EXACTLY, so the finding can be checked.

CRITICAL RULES:
1. Copy phrases and quotes VERBATIM from the document. Do NOT paraphrase,
   normalise, correct or summarise. If you cannot copy it exactly, omit it.
2. Report the document's OWN wording, even where it is unusual. Unusual wording
   is the entire point of this audit -- common wording is already known.
3. Do NOT report a phrase that is only about late payments, default, arrears,
   hardship, or ordinary scheduled repayments. Those are different topics.
4. If the document genuinely says nothing about either topic, return: []
"""


@dataclass
class DiscoveryResult:
    source_id: int
    lender: str
    topic: str
    verified: list[dict] = field(default_factory=list)
    unverified: list[dict] = field(default_factory=list)
    error: str | None = None


def call_discovery_llm(document_text: str) -> str:
    client = _get_client()
    response = client.chat.completions.create(
        model=NVIDIA_MODEL,
        messages=[
            {"role": "system", "content": DISCOVERY_PROMPT},
            {"role": "user", "content": f"Document text:\n\n{document_text[:MAX_DISCOVERY_CHARS]}"},
        ],
        # Generous: the reply carries a verbatim QUOTE per finding, and a
        # document with many hits overruns a small cap mid-string. At 4000 the
        # Westpac source-5 audit was truncated inside a quote and the whole
        # response -- which had already identified a real phrasing ("early
        # termination charge") -- was discarded as unparseable.
        max_tokens=12000,
        temperature=0,
    )
    return response.choices[0].message.content or ""


def find_suspicious_absences(
    session: Session, *, min_chars: int = SUSPICIOUS_ABSENT_CHARS
) -> list[tuple[Source, str]]:
    """
    (source, topic) pairs where the scan found no vocabulary in a document large
    enough that the silence is more likely ours than the document's.
    """
    suspicious = []
    for source in session.query(Source).filter(Source.source_tier == "lender_official").all():
        text = source.raw_content or ""
        if len(text) < min_chars:
            continue
        for topic, keywords in TOPIC_KEYWORDS.items():
            if not find_keyword_hits(text, keywords):
                suspicious.append((source, topic))
    return suspicious


def verify_phrases(document_text: str, items: list[dict], topic: str) -> tuple[list[dict], list[dict]]:
    """
    Split model output into phrases that genuinely occur in the document and those
    that do not. Verbatim occurrence is the whole guarantee here: an unverified
    phrase is a paraphrase or a hallucination, and adding it to TOPIC_KEYWORDS
    would create a keyword that silently never matches.
    """
    lowered = document_text.lower()
    verified, unverified = [], []
    for item in items:
        phrase = (item.get("phrase") or "").strip().lower()
        if not phrase or item.get("topic") != topic:
            continue
        (verified if phrase in lowered else unverified).append(item)
    return verified, unverified


def _parse_possibly_truncated_array(raw: str) -> list[dict]:
    """
    Parse a JSON array, salvaging the complete objects from a truncated response.

    A reply cut off mid-string is not a failed audit -- the findings BEFORE the cut
    are complete, verbatim, and independently verified against the document
    afterwards. Discarding them loses real phrasings for no safety benefit, which
    is what happened to the Westpac source-5 audit.
    """
    start = raw.find("[")
    if start == -1:
        raise ValueError(f"no JSON array in response: {raw[:200]!r}")
    body = raw[start : raw.rfind("]") + 1] if raw.rfind("]") > start else raw[start:]
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass

    # Salvage: keep whole {...} objects, tracking string state so a brace inside a
    # quoted value doesn't end an object early.
    objects, depth, obj_start, in_string, escaped = [], 0, None, False, False
    for i, ch in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                try:
                    objects.append(json.loads(body[obj_start : i + 1]))
                except json.JSONDecodeError:
                    pass
                obj_start = None
    if not objects:
        raise ValueError(f"no parseable objects in response: {raw[:200]!r}")
    logger.warning("SALVAGED %d object(s) from a truncated discovery response", len(objects))
    return objects


def discover_for_source(source: Source, topic: str, *, llm_call=call_discovery_llm) -> DiscoveryResult:
    result = DiscoveryResult(source_id=source.id, lender=source.entity_name, topic=topic)
    text = source.raw_content or ""
    try:
        raw = llm_call(text)
        items = _parse_possibly_truncated_array(raw)
    except Exception as e:  # noqa: BLE001 -- an audit pass must not abort the whole run
        result.error = f"{type(e).__name__}: {e}"
        logger.error("DISCOVERY_FAILED source_id=%s topic=%s -- %s", source.id, topic, result.error)
        return result

    result.verified, result.unverified = verify_phrases(text, items, topic)
    logger.info(
        "DISCOVERY source_id=%s %s topic=%s -> %d verified, %d unverified",
        source.id, source.entity_name, topic, len(result.verified), len(result.unverified),
    )
    return result


def novel_phrases(results: list[DiscoveryResult]) -> dict[str, set[str]]:
    """Verified phrases that TOPIC_KEYWORDS would not already have matched."""
    novel: dict[str, set[str]] = {topic: set() for topic in TOPIC_KEYWORDS}
    for r in results:
        for item in r.verified:
            phrase = item["phrase"].strip().lower()
            if not find_keyword_hits(phrase, TOPIC_KEYWORDS[r.topic]):
                novel[r.topic].add(phrase)
    return novel
