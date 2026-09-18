# Project Context: Australian Debt Policy Database

Read this file in full before doing any work. It explains what we're building, why it's architected this way, and the constraints that shape every decision below. When a task file asks you to do something that seems to conflict with this document, this document wins — ask before proceeding.

## 1. What we're building, at a product level

**North star:** the platform Australians go to for anything financial. We start with debt specifically, since it's the most pressing and validated category, and expand from there.

**Within debt, the scope is broader than mortgage-serviceability alone.** The original wedge (HECS/mortgage serviceability, the CBA example) proved the mechanism works, but the actual product covers the full range of consumer debt decisions: taking on a new loan, refinancing an existing one, switching loan products, consolidating debt, and any of these triggered by a life event (buying a house, a new job, income change, having a child, etc.). The platform doesn't just diagnose a person's situation — it charts the right path for whatever debt decision they're facing and takes them through it end to end.

Four pillars now, not three:

1. **A policy/T&Cs/legislation database** (this is what these tasks build) — structured, queryable data on how Australian banks, credit providers, and legislation actually treat different debt types, beyond headline rates. This now needs to cover refinancing-specific policy (break costs, exit fees, refinance cashback offers, discharge processes) and loan-switching mechanics, not just origination-time serviceability.
2. **Bank account connection via CDR/Open Banking** (not in scope for these tasks) — users connect real accounts for accurate financial data.
3. **A situation modelling/advisory engine** (not in scope for these tasks) — cross-references user data against the database to generate personalised recommendations, now across "should I refinance / take on new debt / switch products / do nothing," not just "how does this affect my borrowing capacity."
4. **An execution/routing layer** (not in scope for these tasks, but now core to the business model, not a maybe-later feature) — once the platform knows what a person should do, it actively connects them to the specific provider/product and takes them through the process, the same function a mortgage broker performs. This is the monetization engine (referral/commission, likely via a mortgage aggregator relationship such as AFG, Connective, Finsure, or Loan Market, which would also provide ACL cover as an Authorised Representative) and it is squarely regulated credit-assistance activity — see section 5, which is now more urgent, not a later concern.

**These tasks are exclusively about pillar 1: the database.** Do not build scraping-time recommendation logic, CDR integration, execution/routing logic, or user-facing UI unless a task explicitly asks for it.

## 2. Why the database is architected the way it is (read this before questioning the schema)

Public bank T&Cs are freely available — that alone is not a moat, since any competitor can scrape the same PDFs. The actual defensibility comes from three things, in order of value:

- **Undocumented/inferred lender policy** — rules that exist in practice but aren't in any public PDF (see the CBA example below). This is the highest-value, hardest-to-replicate layer.
- **Structured queryability** — turning unstructured PDFs/HTML across ~15 sources into a normalized, comparable schema.
- **A reliable maintenance pipeline** — a stale or silently-wrong database is worse than none. The pipeline that keeps this current and auditable is itself part of the moat.

Because of this, the database is split into layers with very different reliability, sourcing, and update characteristics. Don't collapse them into one flat table — that was an explicit early mistake we're avoiding.

### The validated reference case (use this as your test fixture everywhere)

CBA has an undocumented policy: HECS-HELP debt with under 1 year remaining is excluded entirely from borrowing capacity/serviceability calculations. Debt with 1–5 years remaining gets a reduced serviceability buffer (3% down to 1%). A broker modelled a client making a $4,000 voluntary HECS repayment to cross the 5-year threshold; borrowing capacity increased from $490,000 to $600,000 — a $110k swing from one piece of niche policy knowledge.

**Every schema, extraction script, and test suite in this project should be able to represent and correctly answer this case.** If a design can't cleanly encode "CBA + HECS + years_remaining < 1 → excluded from serviceability; 1–5 years → buffer reduced from 3% to 1%, with full source provenance," the design is wrong.

## 3. Scope: the 15 sources, and the broader category coverage they now need to support

- **Big 4 banks:** CBA, Westpac, NAB, ANZ
- **Top 10 non-bank credit providers/lenders** (build the actual list as an early task — don't guess at names; it should reflect current market share among non-bank/second-tier lenders and BNPL providers relevant to mortgages, personal loans, and HECS-adjacent products)
- **Government/legislation sources** (the 5th major category, distinct from lenders):
  - Federal Register of Legislation (legislation.gov.au) — Family Law Act 1975, Corporations Act, National Consumer Credit Protection Act
  - ATO / Study Assist — HECS-HELP indexation, repayment thresholds
  - Services Australia — Centrelink thresholds
  - ASIC — regulatory guidance (kept separate from our own AFSL/ACL compliance research, which is not consumer data)

**Category coverage now needed per lender, beyond origination-time serviceability:** break costs and exit fees on early exit, discharge/switching process and timelines, refinance-specific offers (cashback, rate discounts) and their conditions, and how existing debt across categories (credit cards, personal loans, BNPL, novated leases) is treated in serviceability for a *new or refinanced* loan — not just each product's own standalone T&Cs. This is what supports the broader north star (§1) of guiding refinancing/switching/consolidation decisions, not just new-loan serviceability.

**Explicitly out of scope for now:** state-based taxes/concessions (e.g. stamp duty), full case-law ingestion, the remaining ~85+ smaller ADIs, and any category-4 (execution/routing) build work. Don't silently expand scope to these.

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

## 5. Regulatory context (why we're careful about provenance and confidence — and why this is now urgent, not eventual)

Personalised debt/borrowing recommendations using real financial data are legally "personal financial product advice" under the Corporations Act, and debt management/credit assistance requires an Australian Credit Licence (ACL) since 1 July 2021. This database itself doesn't give advice — but everything we build here needs to support an eventual advisory layer that can defend its recommendations with clear sourcing and confidence levels. This is a large part of *why* sections 4c, 4d, 4f, and 4g exist — don't treat them as optional polish.

**This is no longer a distant concern.** Now that pillar 4 (execution/routing — actively connecting a person to a specific lender/refinance product) is core to the business model, that activity *is* regulated credit assistance, not adjacent to it — the same activity mortgage brokers are licensed for. The realistic path is becoming an Authorised Representative under an existing aggregator's ACL (AFG, Connective, Finsure, Loan Market), which would also solve lender panel access and commission infrastructure in one relationship rather than three separate problems. This decision gates whether pillar 4 can be built at all — treat it as a near-term business decision to pursue in parallel with database work, not something to revisit "later."

## 6. Review workflow (don't auto-merge)

Extraction produces `candidate_rules`. A human (the project owner) reviews and promotes candidates into `production_rules`. At this scale (~15 sources), keep this simple: a script that prints a clear diff/summary for manual review is sufficient. Don't build an approval UI or auto-merge logic.

## 7. What "done" looks like for the database phase

- Schema exists for `sources`, `candidate_rules`, `production_rules`, `rates` (separate from rules), and legislation-specific fields, with the CBA HECS case encoded as a passing test fixture.
- At least the Big 4 banks and the Federal Register of Legislation have working, tested, individually-scoped scrapers.
- Extraction script turns raw scraped text into schema-shaped candidates, schema-validated, with source and prompt-version linkage.
- A review script surfaces candidates (including conflicts) for manual promotion.
- A read-only query interface exists that an agent tool could call against `production_rules` — deterministic, no live fetching.

## 9. Layer 3 verification and corroboration standard

Layer 3 (broker-sourced/undocumented policy) is our highest-value data and our least verifiable data — treat both facts as equally true. A single Reddit post, blog mention, or anecdote is a *lead*, not a fact, no matter how specific or plausible it sounds.

### 9a. Source population
"Broker" in this project means **mortgage brokers** (also called finance brokers, or credit representatives/credit assistance providers in regulatory language) — the people who submit loan applications to multiple lenders and deal directly with lender credit policy and BDMs. Not property/buyer's agents, who have no visibility into lender serviceability policy. Most operate as authorised credit representatives under an aggregator's ACL (AFG, Connective, Finsure, Loan Market, Aussie).

### 9b. Where Layer 3 leads come from (in rough yield order)
0. **Regulator guidance, checked first, not last** — for HECS/HELP specifically, APRA published amendments to Prudential Practice Guide APG 223 and Reporting Standard ARS 223.0 (effective 30 September 2025) formally addressing how ADIs may treat HELP debt in serviceability and DTI reporting. This is `regulator_guidance` tier — higher authority than any individual lender's tacit policy — and generalizes the *pattern* behind the CBA example across the industry. It sets what's *permitted*, not each lender's specific chosen thresholds — those still need lender-specific sourcing, but now anchored to a documented framework rather than discovered from scratch. Before chasing any category of tacit lender policy, check whether an equivalent regulator (APRA, ASIC) guidance document already exists.
1. Broker/brokerage content marketing — SEO blog posts on brokerage firm websites (e.g. "does HECS debt affect your borrowing power"), which are public, scrapable, and often name specific lenders and rules. Confirmed genuinely findable at useful specificity, not just theoretical — e.g. named-broker figures with real dollar comparisons.
2. Trade press — Mortgage Business, The Adviser, Broker News.
3. Broker Facebook groups and LinkedIn — manual mining, same pattern as the original Reddit find.
4. Comparison/consumer-finance sites — Canstar, Mozo, InfoChoice explainer articles.
5. **Calculator probing** — where a lender's public borrowing-power calculator accepts HECS/HELP as a distinct input, systematically varying that input while holding other variables constant produces first-party empirical evidence of that lender's actual policy, sourced from the lender's own tool rather than secondhand description. Stronger evidence than an anecdote where it's available; not every lender's calculator supports this.
6. Direct broker interviews — highest-yield and the only channel that produces *original* corroboration rather than re-aggregating what a scraper could eventually find too.

### 9c. Promotion gate — no rule reaches usable status on a single source
A candidate Layer 3 rule may never be promoted to a status the product will use to generate a specific dollar-figure recommendation while it has only one source. It must clear one of:
- **Corroboration**: a second, independent source (different broker, different platform/source-type, ideally different date) describing the same rule, or
- **Direct confirmation**: a broker directly confirms the specific candidate rule when asked (this is the strongest tier, and worth actively pursuing for existing single-source candidates, not just new leads).

Until then, a rule sits in a `needs_corroboration` status. Rules in this status may be surfaced to a user only as something like "unverified insight — worth confirming with a broker," never as a specific quantified recommendation.

### 9d. Additional verification signals to weigh, not gate on alone
- Platform verification: e.g. r/AskAnAussieBroker enforces flair/mod approval for verified brokers — an unverified account posting is weaker evidence than a flaired one.
- Recency: tag every corroborating source with its date. A rule unconfirmed in the last 6–12 months should be treated as needing re-verification, not permanently settled, since lender policy changes.
- Arithmetic plausibility: where a claim includes a specific numeric consequence (like the CBA $490k→$600k example), sanity-check it against standard serviceability-calculator assumptions before trusting the claim at face value — a mechanically implausible number is a reason to downgrade confidence even if the source seems credible.

### 9e. Scope discipline for Layer 3 sourcing effort
Layer 3 sourcing (broker content, interviews, etc.) is worth the effort for **serviceability/decisioning policy** — undocumented rules that affect how a lender assesses or prices an application — across **all debt categories the platform now covers** (mortgages, personal loans, credit cards, refinancing, consolidation), not mortgages alone. The distinction that matters is not "which product" but "published vs. tacit": a card's own fees/redraw/rate T&Cs are genuinely public (Layer 1/2 covers them); how that *same* card's limit factors into a *mortgage* lender's serviceability calculation is tacit (Layer 3). Don't spend Layer 3 effort re-deriving a product's own public T&Cs — do spend it on how any debt type is treated when a lender is deciding on *another* application, which is where the undocumented, high-dollar-impact rules live regardless of which product triggered the search.

## 11. The execution-layer north star (context, not a build target yet)

Worth understanding even though nothing here is built in these tasks: the long-term shape of the product is closer to Credit Karma's trajectory than a static comparison site — start as the free diagnostic/credit-health layer people trust, then become the place they're actually routed to act (refinance, new loan, switch product), monetized via referral/commission to the provider, most likely through a mortgage aggregator relationship for licence cover and lender-panel access. Debt is the starting wedge; the destination is broader financial products generally.

This matters for database work now in two concrete ways:
- **Coverage breadth**: the database needs to support refinancing and switching decisions, not just origination-time serviceability (see §3's category-coverage note) — a scenario like "should this person refinance right now" needs break-cost/exit-fee/discharge-timeline data that origination-only scraping wouldn't have prioritized.
- **Data quality bar**: because pillar 4 will eventually act on this data (routing a real person to a real lender), the corroboration/confidence discipline in §9 isn't academic rigor for its own sake — it's the actual gate on what the platform can responsibly recommend once execution exists. Don't relax it because pillar 4 feels far away; it's the reason §9 exists.

Do not start building pillar 4 (execution/routing, lender API integration, ApplyOnline/NextGen-style application submission, aggregator-partnership tooling) from this file alone — that needs its own context/task pass once the business-side aggregator relationship is settled.

## 12. Working style expectations

- Prefer several small, independently testable modules over one large pipeline.
- Write tests before or alongside implementation wherever the task says to, especially for schema and extraction work — the CBA fixture should appear in the test suite early and stay there.
- Don't expand scope beyond what a task specifies (e.g., don't start building CDR integration or the advisory engine "while you're at it").
- If something in a task conflicts with this file, flag it rather than silently choosing one.