"""
Turns a `sources` row's raw text into schema-shaped `candidate_rules`, using an
LLM to identify only rules the document explicitly states. Never writes to
`production_rules` -- a human reviews and promotes candidates separately (section 6).
"""

import json
import logging
import os
import time
from datetime import date, datetime

from dotenv import load_dotenv
from openai import APIError, OpenAI
from sqlalchemy.orm import Session

from db.models import POLICY_AREAS, CandidateRule, Source

logger = logging.getLogger("ingestion.extraction")

EXTRACTION_PROMPT_VERSION = "1"

# About a third of real documents were observed hitting malformed/unparseable
# JSON from the model on a given attempt (not a property of the input -- an
# identical retry with the same input has recovered every time this was
# checked manually). MAX_EXTRACTION_ATTEMPTS = 1 initial attempt + 2 retries.
MAX_EXTRACTION_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1.0

# Large documents are extracted in overlapping chunks rather than one call.
# This is NOT a token-limit workaround -- it is a wall-clock one. NVIDIA's
# gateway returns 504 after about 300 seconds regardless of our client timeout,
# and a 69k-char product guide asking for "every explicit rule" cannot finish
# generating inside that budget. Raising max_tokens made it strictly worse: more
# output to generate against the same server deadline. The only fix that works is
# less work per call.
#
# 20k is chosen to sit well inside the observed budget: the v2 excerpt pass
# routinely completed 24k-char inputs in 60-210 seconds.
MAX_CHUNK_CHARS = 20000
# Overlap so a rule straddling a boundary is not cut in half. Duplicates created
# by the overlap are removed after extraction.
CHUNK_OVERLAP_CHARS = 2000


class ExtractionFailedError(RuntimeError):
    """
    Raised when the LLM never returned parseable JSON after MAX_EXTRACTION_ATTEMPTS
    tries. Deliberately a distinct exception type from a clean empty-array result --
    callers need to be able to tell "this document genuinely had nothing extractable"
    (extract_candidates returns []) apart from "the model failed to respond usably"
    (this is raised) rather than conflating both into "0 candidates".
    """

NVIDIA_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

# source_tier -> confidence tier, per CONTEXT.md 4f. Extraction from an official
# document (statute, regulation, regulator guidance, or a lender's own PDS/T&Cs)
# starts at official_document confidence; broker-sourced material starts lower.
CONFIDENCE_BY_SOURCE_TIER = {
    "statute": "official_document",
    "regulation": "official_document",
    "regulator_guidance": "official_document",
    "lender_official": "official_document",
    "broker_sourced": "single_anecdotal_source",
}

SYSTEM_PROMPT = """detailed thinking off

You are a precise information-extraction system for an Australian
debt-policy database. Given the raw text of ONE source document, extract every
EXPLICIT, structured lending/credit rule it states.

Return ONLY a JSON array (no prose, no markdown code fences). Each element is an
object with exactly these fields:
- "debt_type": string category, e.g. "home_loan", "personal_loan", "credit_card",
  "HECS_HELP", "BNPL" -- null if the document does not clearly state which debt
  type a rule applies to
- "conditions": an object describing when the rule applies, using whatever keys
  best fit the specific numbers/thresholds the text states (e.g.
  {"min_deposit_pct": 20}) -- {} if the rule is unconditional
- "effect": an object describing what happens when conditions are met (e.g.
  {"fee_waived": true, "fee_amount_aud": 350}) -- {} if nothing concrete is stated
- "effective_from": ISO date string (YYYY-MM-DD) ONLY if the document explicitly
  states one, else null
- "effective_to": ISO date string ONLY if explicitly stated, else null
- "legislation_status": one of "proposed", "introduced_to_parliament", "enacted",
  "in_force" -- ONLY if this document is itself a piece of legislation and states
  its status, else null
- "enacted_date": ISO date, only for legislation, else null
- "commencement_date": ISO date, only for legislation, else null

CRITICAL RULES -- follow these exactly:
1. Extract ONLY rules that are explicitly and specifically stated in the given
   text. Do NOT use outside knowledge about this lender, industry norms, or
   common practice, even if you believe you know it.
2. If a field's value is not stated in the text, its value MUST be null (or {}
   for conditions/effect). NEVER guess, infer, estimate, or fill in a
   plausible-sounding value to complete the schema.
3. If the text contains no extractable structured rule at all, return an empty
   JSON array: []
4. Do not attribute any rule to a lender or entity other than the one named in
   the user message.

The following shows the REQUIRED OUTPUT SHAPE ONLY -- it is unrelated to the
actual document you will be given and must not influence what you extract:
[{"debt_type": "personal_loan", "conditions": {"early_repayment": true}, "effect": {"fee_charged": false}, "effective_from": null, "effective_to": null, "legislation_status": null, "enacted_date": null, "commencement_date": null}]
"""


# Must be sized against max_tokens, not chosen independently. At 300s a 69k-char
# document generating up to 32000 tokens timed out mid-generation on every attempt:
# raising the token cap without raising this traded a truncation bug for a latency one.
REQUEST_TIMEOUT_SECONDS = 900.0


def _get_client() -> OpenAI:
    load_dotenv(dotenv_path=".env")
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        raise RuntimeError("NVIDIA_API_KEY not set (check .env)")
    return OpenAI(base_url=NVIDIA_BASE_URL, api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS)


def call_llm(entity_name: str, source_text: str, system_prompt: str | None = None) -> str:
    """Send one extraction request. Returns the raw model response text."""
    client = _get_client()
    response = client.chat.completions.create(
        model=NVIDIA_MODEL,
        messages=[
            {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
            {"role": "user", "content": f"Entity: {entity_name}\n\nDocument text:\n\n{source_text}"},
        ],
        # Sized for ONE CHUNK, not a whole document. 32000 was set when a single
        # call had to cover an entire 69k-char guide; with chunking, a large cap
        # only lengthens generation against the gateway's ~300s deadline and
        # causes 504s. Truncation is still handled by _parse_json_array's salvage.
        max_tokens=12000,
        temperature=0,
    )
    return response.choices[0].message.content or ""


def _parse_json_array(raw_text: str) -> list[dict]:
    """
    Parse the model's JSON array, salvaging complete objects from a truncated reply.

    A response cut off by max_tokens is not a failed extraction: every object BEFORE
    the cut is complete and well-formed. Discarding the whole reply throws those away
    and -- because temperature is 0 -- the retries truncate in the same place, so all
    MAX_EXTRACTION_ATTEMPTS burn on an identical failure. This was observed on Pepper
    Money source 15, a 69k-char product guide whose output ran past 44,000 characters.

    Salvage is deliberately conservative: it keeps only whole top-level {...} objects
    and tracks string state, so a brace inside a quoted value can't end an object
    early. A reply with no complete object at all still raises.
    """
    start = raw_text.find("[")
    if start == -1:
        raise ValueError(f"No JSON array found in LLM response: {raw_text[:500]!r}")

    end = raw_text.rfind("]")
    body = raw_text[start : end + 1] if end > start else raw_text[start:]
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass

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
        raise ValueError(f"No parseable objects in LLM response: {raw_text[:500]!r}")
    logger.warning(
        "SALVAGED %d complete object(s) from a truncated extraction response "
        "(raw length %d chars) -- raise max_tokens if this recurs",
        len(objects), len(raw_text),
    )
    return objects


def _parse_date(value) -> date | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _extract_one_chunk(source, chunk: str, llm_call, index: int, total: int) -> list[dict] | None:
    """
    One chunk, with the retry policy. Returns parsed items, or None if every
    attempt failed for this chunk.

    A failed chunk does NOT fail the document: on a 6-chunk guide, losing one
    chunk to a transient 504 should cost that chunk's rules, not the other five.
    extract_candidates() raises ExtractionFailedError only if EVERY chunk failed.
    """
    last_error: Exception | None = None
    for attempt in range(1, MAX_EXTRACTION_ATTEMPTS + 1):
        try:
            return _parse_json_array(llm_call(source.entity_name, chunk))
        # APIError covers timeouts, rate limits and 5xx -- the transient failures
        # retrying exists for. The llm_call was originally outside the try, so a
        # single read timeout propagated out of extract_candidates and killed a
        # whole multi-document run.
        except (ValueError, json.JSONDecodeError, APIError) as e:
            last_error = e
            logger.warning(
                "RETRY attempt=%d/%d entity=%s source_id=%s chunk=%d/%d -- %s: %s",
                attempt, MAX_EXTRACTION_ATTEMPTS, source.entity_name, source.id,
                index, total, type(e).__name__, e,
            )
            if attempt < MAX_EXTRACTION_ATTEMPTS:
                # Exponential: an immediate retry after a timeout or rate limit
                # usually reproduces it.
                time.sleep(RETRY_DELAY_SECONDS * (2 ** (attempt - 1)))

    logger.error(
        "CHUNK_FAILED entity=%s source_id=%s chunk=%d/%d -- %s: %s",
        source.entity_name, source.id, index, total, type(last_error).__name__, last_error,
    )
    return None


def _chunk_text(text: str, size: int = MAX_CHUNK_CHARS, overlap: int = CHUNK_OVERLAP_CHARS) -> list[str]:
    """
    Split into overlapping chunks, preferring a paragraph or sentence boundary so a
    rule is not severed mid-clause. Returns [text] unchanged when it already fits,
    so small documents behave exactly as before.
    """
    if len(text) <= size:
        return [text]

    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[max(start, end - 1500):end]
            for sep in ("\n\n", ".\n", ". "):
                cut = window.rfind(sep)
                if cut != -1:
                    end = max(start, end - 1500) + cut + len(sep)
                    break
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def extract_candidates(
    session: Session,
    source: Source,
    *,
    llm_call=call_llm,
    prompt_version: str = EXTRACTION_PROMPT_VERSION,
    text_override: str | None = None,
    default_policy_area: str | None = None,
) -> list[CandidateRule]:
    """
    Extract candidate rules from `source.raw_content` and insert them into
    `candidate_rules` (never `production_rules`). Returns the inserted rows.

    !! THIS FUNCTION FLUSHES BUT DOES NOT COMMIT. The caller MUST commit. !!
    That's deliberate -- change_detection.check_lender_source() needs the new
    `sources` row and its candidates to land in one atomic transaction, so this
    can't commit on its own. But it is a genuine footgun: a long extraction run
    that never commits loses every LLM call it paid for the moment the process
    dies, which is exactly what happened to the first version of the v2
    loan-mechanics pass. If you write a new caller, commit -- and commit
    incrementally, not once at the end of a multi-document run.

    `llm_call` is injectable so tests can supply a canned response instead of
    hitting a real API. To run a DIFFERENT prompt against the same source, pass
    `functools.partial(call_llm, system_prompt=...)` plus the matching
    `prompt_version` -- that pairing is what 4i's versioning exists for, so the
    two must always be set together.

    `text_override` sends something other than the full raw_content to the model
    (e.g. only the excerpts relevant to a targeted extraction pass). The stored
    candidate still links to the full source record, so provenance is unaffected.

    `default_policy_area` is applied to any extracted item that doesn't state its
    own; an item may also return a "policy_area" field, which is validated
    against POLICY_AREAS and wins over the default.
    """
    if not source.raw_content:
        raise ValueError(f"source {source.id} has no raw_content to extract from")

    source_text = text_override if text_override is not None else source.raw_content

    chunks = _chunk_text(source_text)
    if len(chunks) > 1:
        logger.info(
            "CHUNKED entity=%s source_id=%s %d chars -> %d chunks",
            source.entity_name, source.id, len(source_text), len(chunks),
        )

    items: list[dict] | None = None
    for chunk_index, chunk in enumerate(chunks, start=1):
        chunk_items = _extract_one_chunk(source, chunk, llm_call, chunk_index, len(chunks))
        if chunk_items is None:
            continue
        items = (items or []) + chunk_items

    if items is not None:
        # Overlap between chunks re-presents the same clauses, so the same rule can
        # come back twice. Deduped on the full payload, since two genuinely
        # different rules will differ somewhere in it.
        seen, deduped = set(), []
        for item in items:
            key = json.dumps(item, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                deduped.append(item)
        if len(deduped) < len(items):
            logger.info(
                "CHUNK_DEDUPE entity=%s source_id=%s dropped %d overlap duplicate(s)",
                source.entity_name, source.id, len(items) - len(deduped),
            )
        items = deduped

    if items is None:
        # Every chunk failed. Still distinct from a clean empty result: callers
        # rely on this exception to tell "the document had nothing extractable"
        # apart from "the model never answered".
        logger.error(
            "EXTRACTION_FAILED_AFTER_RETRIES entity=%s source_id=%s chunks=%d -- all chunks failed",
            source.entity_name, source.id, len(chunks),
        )
        raise ExtractionFailedError(
            f"Extraction failed for every one of {len(chunks)} chunk(s) of source id={source.id} "
            f"(entity={source.entity_name!r}) after {MAX_EXTRACTION_ATTEMPTS} attempts each."
        )

    confidence = CONFIDENCE_BY_SOURCE_TIER[source.source_tier]

    inserted = []
    for item in items:
        debt_type = item.get("debt_type")
        if not debt_type:
            # debt_type is a required column -- a candidate with no debt type
            # attached isn't usable, so skip it rather than invent one.
            continue

        # An item may name its own policy_area; anything unrecognised is dropped
        # back to the default rather than written through, since a bad enum value
        # would fail the insert anyway and a guessed one would be worse than none.
        item_policy_area = item.get("policy_area")
        if item_policy_area not in POLICY_AREAS:
            item_policy_area = None

        candidate = CandidateRule(
            lender=source.entity_name,
            debt_type=debt_type,
            policy_area=item_policy_area or default_policy_area,
            conditions=item.get("conditions") or {},
            effect=item.get("effect") or {},
            source_tier=source.source_tier,
            legislation_status=item.get("legislation_status"),
            enacted_date=_parse_date(item.get("enacted_date")),
            commencement_date=_parse_date(item.get("commencement_date")),
            # If the document doesn't state an effective date, default to when we
            # observed it -- that's our own bookkeeping, not a guessed fact about
            # the document's content.
            effective_from=_parse_date(item.get("effective_from")) or source.retrieved_date,
            effective_to=_parse_date(item.get("effective_to")),
            confidence=confidence,
            conflicting_sources=False,
            extraction_prompt_version=prompt_version,
            source_id=source.id,
        )
        session.add(candidate)
        inserted.append(candidate)

    session.flush()
    return inserted


if __name__ == "__main__":
    from db.schema import create_all
    from db.session import get_session

    create_all()
    session = get_session()

    cba_sources = session.query(Source).filter(Source.entity_name == "CBA").all()
    if not cba_sources:
        raise SystemExit("No CBA sources found -- run ingestion/scrapers/cba.py first.")

    for source in cba_sources:
        print(f"\n--- Extracting from source id={source.id} url={source.url} ---")
        candidates = extract_candidates(session, source)
        if not candidates:
            print("No candidates extracted.")
        for c in candidates:
            print(
                f"  debt_type={c.debt_type!r} conditions={c.conditions!r} "
                f"effect={c.effect!r} effective_from={c.effective_from} confidence={c.confidence}"
            )
    session.commit()
