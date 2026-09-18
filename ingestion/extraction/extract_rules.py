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
from openai import OpenAI
from sqlalchemy.orm import Session

from db.models import CandidateRule, Source

logger = logging.getLogger("ingestion.extraction")

EXTRACTION_PROMPT_VERSION = "1"

# About a third of real documents were observed hitting malformed/unparseable
# JSON from the model on a given attempt (not a property of the input -- an
# identical retry with the same input has recovered every time this was
# checked manually). MAX_EXTRACTION_ATTEMPTS = 1 initial attempt + 2 retries.
MAX_EXTRACTION_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1.0


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


REQUEST_TIMEOUT_SECONDS = 300.0


def _get_client() -> OpenAI:
    load_dotenv(dotenv_path=".env")
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        raise RuntimeError("NVIDIA_API_KEY not set (check .env)")
    return OpenAI(base_url=NVIDIA_BASE_URL, api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS)


def call_llm(entity_name: str, source_text: str) -> str:
    """Send one extraction request. Returns the raw model response text."""
    client = _get_client()
    response = client.chat.completions.create(
        model=NVIDIA_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Entity: {entity_name}\n\nDocument text:\n\n{source_text}"},
        ],
        max_tokens=16000,
        temperature=0,
    )
    return response.choices[0].message.content or ""


def _parse_json_array(raw_text: str) -> list[dict]:
    start = raw_text.find("[")
    end = raw_text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"No JSON array found in LLM response: {raw_text[:500]!r}")
    return json.loads(raw_text[start : end + 1])


def _parse_date(value) -> date | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def extract_candidates(
    session: Session,
    source: Source,
    *,
    llm_call=call_llm,
) -> list[CandidateRule]:
    """
    Extract candidate rules from `source.raw_content` and insert them into
    `candidate_rules` (never `production_rules`). Returns the inserted rows.

    `llm_call` is injectable so tests can supply a canned response instead of
    hitting a real API.
    """
    if not source.raw_content:
        raise ValueError(f"source {source.id} has no raw_content to extract from")

    items = None
    last_error: Exception | None = None
    for attempt in range(1, MAX_EXTRACTION_ATTEMPTS + 1):
        raw_response = llm_call(source.entity_name, source.raw_content)
        try:
            items = _parse_json_array(raw_response)
            break
        except (ValueError, json.JSONDecodeError) as e:
            last_error = e
            logger.warning(
                "RETRY attempt=%d/%d entity=%s source_id=%s -- unparseable response: %s: %s",
                attempt, MAX_EXTRACTION_ATTEMPTS, source.entity_name, source.id, type(e).__name__, e,
            )
            if attempt < MAX_EXTRACTION_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS)

    if items is None:
        logger.error(
            "EXTRACTION_FAILED_AFTER_RETRIES entity=%s source_id=%s attempts=%d -- %s: %s",
            source.entity_name, source.id, MAX_EXTRACTION_ATTEMPTS, type(last_error).__name__, last_error,
        )
        raise ExtractionFailedError(
            f"Extraction failed after {MAX_EXTRACTION_ATTEMPTS} attempts for source id={source.id} "
            f"(entity={source.entity_name!r}): {type(last_error).__name__}: {last_error}"
        ) from last_error

    confidence = CONFIDENCE_BY_SOURCE_TIER[source.source_tier]

    inserted = []
    for item in items:
        debt_type = item.get("debt_type")
        if not debt_type:
            # debt_type is a required column -- a candidate with no debt type
            # attached isn't usable, so skip it rather than invent one.
            continue

        candidate = CandidateRule(
            lender=source.entity_name,
            debt_type=debt_type,
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
            extraction_prompt_version=EXTRACTION_PROMPT_VERSION,
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
