# Australian Debt Policy Database

This project builds a structured, queryable database of how Australian banks, non-bank
lenders, and legislation actually treat different debt types — going beyond headline
advertised rates to capture the undocumented and inferred policy detail that matters in
practice (e.g. how a bank treats HECS-HELP debt in serviceability calculations).

It is one of three pillars of a larger debt-advisory platform (the others being bank
account connection via CDR/Open Banking, and a personalised advisory engine) — but this
repository is scoped **only** to the database itself: scraping and caching raw sources,
extracting structured candidate rules, and letting a human review and promote them into
a production dataset that a future advisory tool can query deterministically.

Key design points this repo follows (see `CONTEXT.md` for full detail):

- Structured data is stored and served from the database; raw sources are scraped and
  cached on a schedule, never fetched live in response to a query.
- Fast-moving interest rates and slow-moving structural policy rules live in separate
  tables with separate refresh cadences.
- Every rule carries a source authority tier (statute > regulation > regulator_guidance
  > lender_official > broker_sourced), a confidence level, and full provenance back to
  the raw source document.
- Legislative sources track a status lifecycle (proposed → introduced → enacted → in
  force) with commencement date tracked separately from enactment date.
- Rule changes are versioned with effective-date ranges, never overwritten.
- Extraction produces `candidate_rules`; a human reviews and promotes them into
  `production_rules` — no auto-merge.
- The CBA HECS-HELP serviceability treatment (exclusion under 1 year remaining, reduced
  buffer for 1–5 years) is the reference test fixture used throughout.

## Layout

- `ingestion/scrapers/` — one module per source (Big 4 banks, non-bank lenders,
  legislation.gov.au, ATO/Study Assist, Services Australia, ASIC).
- `ingestion/extraction/` — turns raw scraped text into schema-shaped candidate rules,
  plus the manual review script.
- `db/` — schema, models, session handling, and the read-only query interface.
- `tests/` — schema and extraction tests, including the CBA HECS fixture.
- `sources/` — gitignored cache of raw scraped documents (PDFs/HTML).

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Run tests with:

```bash
pytest
```
