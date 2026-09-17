# Project Context: Australian Debt Policy Database

Read this file in full before doing any work. It explains what we're building, why it's architected this way, and the constraints that shape every decision below. When a task file asks you to do something that seems to conflict with this document, this document wins — ask before proceeding.

## 1. What we're building, at a product level

An Australian debt tracking and advisory platform with three pillars:

1. **A policy/T&Cs/legislation database** (this is what these tasks build) — structured, queryable data on how Australian banks, credit providers, and legislation actually treat different debt types, beyond headline rates.
2. **Bank account connection via CDR/Open Banking** (not in scope for these tasks) — users connect real accounts for accurate financial data.
3. **A situation modelling/advisory engine** (not in scope for these tasks) — cross-references user data against the database to generate personalised recommendations.

**These tasks are exclusively about pillar 1: the database.** Do not build scraping-time recommendation logic, CDR integration, or user-facing UI unless a task explicitly asks for it.

## 2. Why the database is architected the way it is (read this before questioning the schema)

Public bank T&Cs are freely available — that alone is not a moat, since any competitor can scrape the same PDFs. The actual defensibility comes from three things, in order of value:

- **Undocumented/inferred lender policy** — rules that exist in practice but aren't in any public PDF (see the CBA example below). This is the highest-value, hardest-to-replicate layer.
- **Structured queryability** — turning unstructured PDFs/HTML across ~15 sources into a normalized, comparable schema.
- **A reliable maintenance pipeline** — a stale or silently-wrong database is worse than none. The pipeline that keeps this current and auditable is itself part of the moat.

Because of this, the database is split into layers with very different reliability, sourcing, and update characteristics. Don't collapse them into one flat table — that was an explicit early mistake we're avoiding.

### The validated reference case (use this as your test fixture everywhere)

CBA has an undocumented policy: HECS-HELP debt with under 1 year remaining is excluded entirely from borrowing capacity/serviceability calculations. Debt with 1–5 years remaining gets a reduced serviceability buffer (3% down to 1%). A broker modelled a client making a $4,000 voluntary HECS repayment to cross the 5-year threshold; borrowing capacity increased from $490,000 to $600,000 — a $110k swing from one piece of niche policy knowledge.

**Every schema, extraction script, and test suite in this project should be able to represent and correctly answer this case.** If a design can't cleanly encode "CBA + HECS + years_remaining < 1 → excluded from serviceability; 1–5 years → buffer reduced from 3% to 1%, with full source provenance," the design is wrong.

## 3. Scope: the 15 sources

- **Big 4 banks:** CBA, Westpac, NAB, ANZ
- **Top 10 non-bank credit providers/lenders** (build the actual list as an early task — don't guess at names; it should reflect current market share among non-bank/second-tier lenders and BNPL providers relevant to mortgages, personal loans, and HECS-adjacent products)
- **Government/legislation sources** (the 5th major category, distinct from lenders):
  - Federal Register of Legislation (legislation.gov.au) — Family Law Act 1975, Corporations Act, National Consumer Credit Protection Act
  - ATO / Study Assist — HECS-HELP indexation, repayment thresholds
  - Services Australia — Centrelink thresholds
  - ASIC — regulatory guidance (kept separate from our own AFSL/ACL compliance research, which is not consumer data)

**Explicitly out of scope for now:** state-based taxes/concessions (e.g. stamp duty), full case-law ingestion, the remaining ~85+ smaller ADIs. Don't silently expand scope to these.

## 4. Core architectural decisions (non-negotiable unless a task says otherwise)

### 4a. Store structured data, cache raw sources — never fetch live per query
Runtime advisory queries hit structured tables only. Raw PDFs/HTML are scraped and cached on a schedule (or on detected change), never fetched live in response to a user question. This is for latency, determinism (same question → same answer), auditability, and testability.

### 4b. Rates and rules are different pipelines with different cadences
Interest rates change constantly (daily/weekly) and are commodity data. Structural policy rules (redraw terms, serviceability treatment, HECS buffers) change rarely (1–4x/year) and are our actual moat. **Keep these in separate tables with separate refresh schedules.** Don't build one pipeline that treats both the same way.

### 4c. Source authority hierarchy
Legislation overrides lender policy when they conflict. Every rule record needs a `source_tier` field with this ordering (highest authority first):
```
statute > regulation > regulator_guidance > lender_official > broker_sourced
```
When two sources at different tiers disagree, the higher tier wins automatically. When two sources at the *same* tier disagree, flag for manual review (see 4e).

### 4d. Legislation has a status lifecycle distinct from lender docs
Government/legislative sources need a `status` field: `proposed` → `introduced_to_parliament` → `enacted` → `in_force`, with **commencement date tracked separately from enactment date**. Never present a `proposed` rule as authoritative. This matters concretely for HECS indexation, which is often announced in the Budget months before being legislated (and sometimes amended or not passed at all).

### 4e. Versioning, not overwriting
Every rule change creates a new version with an effective-date range (`effective_from`, `effective_to`), never overwrites the previous value. This supports point-in-time queries ("what was true as of date X") and lets us explain historical decisions accurately.

### 4f. Confidence and conflict flags are first-class fields, not afterthoughts
Every rule record needs:
- `confidence` tier — distinguish at minimum: `official_document`, `corroborated_broker_source` (2+ independent sources), `single_anecdotal_source`
- `conflicting_sources: bool` + a note field, for same-tier disagreements
This is what will later let the advisory engine decide whether a rule is safe to surface as a recommendation versus "worth investigating further."

### 4g. Provenance on everything
Every structured rule record must link back to a `sources` record containing the raw document/page, retrieved-date, and URL. No rule should exist without a traceable source — this matters both for quality control and because it's part of our regulatory defensibility story (we can show where a claim came from).

### 4h. Entity resolution
Some lender brands are white-labels or subsidiaries of the same underlying ADI, sharing the same actual servicing/credit policy despite separate customer-facing docs. Resolve this explicitly per lender (a `parent_entity` field or similar) so we don't manufacture false "conflicts" between two brands with identical upstream policy.

### 4i. Extraction is versioned too
Every extracted candidate rule records which `extraction_prompt_version` produced it. When we improve the extraction prompt, we need to know which existing records were extracted with an old version and may need re-extraction.

### 4j. Security boundary
This repo/database handles only *public* policy and legislation data. It must be architecturally separated from any future user CDR/bank-account data — different database, different credentials, no shared tables — even though CDR integration isn't part of these tasks. Don't add any user-data tables here.

## 5. Regulatory context (why we're careful about provenance and confidence)

Personalised debt/borrowing recommendations using real financial data are legally "personal financial product advice" under the Corporations Act, and debt management/credit assistance requires an Australian Credit Licence (ACL) since 1 July 2021. This database itself doesn't give advice — but everything we build here needs to support an eventual advisory layer that can defend its recommendations with clear sourcing and confidence levels. This is a large part of *why* sections 4c, 4d, 4f, and 4g exist — don't treat them as optional polish.

## 6. Review workflow (don't auto-merge)

Extraction produces `candidate_rules`. A human (the project owner) reviews and promotes candidates into `production_rules`. At this scale (~15 sources), keep this simple: a script that prints a clear diff/summary for manual review is sufficient. Don't build an approval UI or auto-merge logic.

## 7. What "done" looks like for the database phase

- Schema exists for `sources`, `candidate_rules`, `production_rules`, `rates` (separate from rules), and legislation-specific fields, with the CBA HECS case encoded as a passing test fixture.
- At least the Big 4 banks and the Federal Register of Legislation have working, tested, individually-scoped scrapers.
- Extraction script turns raw scraped text into schema-shaped candidates, schema-validated, with source and prompt-version linkage.
- A review script surfaces candidates (including conflicts) for manual promotion.
- A read-only query interface exists that an agent tool could call against `production_rules` — deterministic, no live fetching.

## 8. Working style expectations

- Prefer several small, independently testable modules over one large pipeline.
- Write tests before or alongside implementation wherever the task says to, especially for schema and extraction work — the CBA fixture should appear in the test suite early and stay there.
- Don't expand scope beyond what a task specifies (e.g., don't start building CDR integration or the advisory engine "while you're at it").
- If something in a task conflicts with this file, flag it rather than silently choosing one.