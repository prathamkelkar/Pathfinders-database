# Source Inventory

Research pass compiled 2026-09-17. Covers Big 4 banks, the top-10 non-bank
lenders/BNPL shortlist (justified below), and government/legislation sources
per CONTEXT.md section 3.

**Legend for "Serviceability policy publicly documented?"**
- **No** — only contractual T&Cs/PDS/Credit Guide found; underwriting/serviceability
  methodology is not public. These entities are still scraper targets for their
  PDS/T&Cs (structured queryability layer), but their serviceability treatment is a
  **Layer 3 candidate** (undocumented/inferred policy) sourced via broker/press
  reporting, not their own docs.
- **Partial** — some genuine servicing-policy content is public, but incomplete,
  stale, or third-party-mirrored.
- **N/A** — government/legislation sources; not applicable, this column concerns
  lender underwriting policy specifically.

## Top-10 non-bank lender/BNPL shortlist — reasoning

Justified by loan book size / origination volume / market share from 2024-2025
industry reporting (The Adviser, company results, ASX filings), not assumption.
Category mix: 5 mortgage non-banks, 3 personal-loan non-banks, 2 BNPL.

| # | Name | Evidence | Source |
|---|------|----------|--------|
| 1 | La Trobe Financial | ~$20bn AUM, largest non-bank credit fund manager in AU | latrobefinancial.com.au news release |
| 2 | Pepper Money | $18.2bn loan book; H1 2025 originations $6.3bn (+40% YoY) | The Adviser, non-bank scaling roundup |
| 3 | Resimac | $14.7bn home loan AUM; FY26 originations $6.7bn (+16%) | The Adviser, same roundup |
| 4 | Liberty Financial | Record FY26 group originations $6.11bn | The Adviser, same roundup |
| 5 | Firstmac | >$15bn loans under management — **2023 figure, likely stale, weakest evidence in the list** | MPA Firstmac profile |
| 6 | Latitude Financial | Largest ASX-listed consumer finance non-bank; FY25 originations $9.1bn (+10%) | The Adviser, Latitude profit report |
| 7 | MoneyMe | $1.5bn loan book; +65% YoY originations Q3 2025 | TipRanks company announcement |
| 8 | Wisr | $110.0m quarterly originations to 31 Mar 2025, +115% YoY | The Adviser, Wisr originations report |
| 9 | Afterpay (Block) | Industry reports name it a dominant BNPL player — **AU-specific GMV not isolated from global Block figures, weak evidence** | BusinessWire/ResearchAndMarkets AU BNPL report |
| 10 | Zip Co | FY25 revenue $278.9m (+26.5% YoY); newly ASIC-licensed post June 2025 BNPL reform | Kalkine, Zip Co FY25 results |

**Runners-up considered and excluded** (kept here for traceability, not scraper
targets): Plenti ($3.1bn portfolio, +32% YoY — legitimate #11, smaller than the
chosen personal-loan trio), Harmoney (smaller AU-specific book), Bluestone
Mortgages (frequently mentioned alongside #1-4 but no current sourced
AUM/origination figure found), Humm (named a top-3 BNPL player in market
commentary but no current hard number found — possible future swap for Afterpay
if better data surfaces).

## Full source table

| Name | Category | Product types | URL(s) | Format | Update cadence (best guess) | Serviceability policy publicly documented? |
|---|---|---|---|---|---|---|
| CBA | Big 4 | Home loan | [UTC Home Loan T&Cs](https://www.commbank.com.au/content/dam/commbank/personal/apply-online/download-printed-forms/utc-home-loan.pdf); [Home Loan Customer Guide](https://www.commbank.com.au/content/dam/commbank-assets/home-loans/docs/commbank-home-loan-customer-guide.pdf) | PDF | Reissued on rate/policy change, dated by effective date | No — HECS treatment (disregard debt if payoff <12mo; reduced buffer for debt payable within 5yrs) reported only via press/broker channels, not a published policy doc → Layer 3 candidate |
| Westpac | Big 4 | Home loan, personal loan | [T&Cs hub](https://www.westpac.com.au/terms-conditions/); [Flexi Loan Conditions](https://www.westpac.com.au/content/dam/public/wbc/documents/pdf/pb/Flexi_Loan_Conditions.pdf); [Personal Loan Contract T&Cs](https://www.westpac.com.au/content/dam/public/wbc/documents/pdf/pb/personal-loans/p-l-contract-general-conditions-180324.pdf) | PDF + HTML | Dated per revision, reissued on rate/product change | No → Layer 3 candidate |
| NAB | Big 4 | Home loan | [T&Cs hub](https://www.nab.com.au/personal/home-loans/terms-conditions); [Home Loan General Terms](https://www.nab.com.au/content/dam/nabrwd/documents/terms-and-conditions/loans/home-loan-general-terms.pdf); [Choice Package T&Cs](https://www.nab.com.au/content/dam/nabrwd/documents/terms-and-conditions/loans/nab-choice-package-terms-conditions.pdf) | PDF + HTML | Effective-date versioned | No, but the policy *change itself* is publicly announced: from 31 Jul 2025 NAB disregards HELP/HECS debt up to $20,000 entirely — see [NAB consumer article](https://www.nab.com.au/personal/life-moments/home-property/buy-first-home/hecs-home-loan) → Layer 3 candidate |
| ANZ | Big 4 | Home loan, consumer lending | [Consumer Lending T&Cs, V39](https://www.anz.com.au/content/dam/anzcomau/documents/pdf/consumer-lending-tc.pdf); [Fees & terms hub](https://www.anz.com.au/support/legal/rates-fees-terms/fees-terms-conditions/personal-home-loans/) | PDF (versioned) + HTML | Explicitly version-numbered, reissued on regulatory/rate change | No, no ANZ-specific HECS document located → Layer 3 candidate |
| Pepper Money | Non-bank, mortgage | Home loan | [Retail Product Guide](https://www.pepperbroker.com.au/content/dam/aubroker/broker-au-documents-nc/Pepper%20Money%20Retail%20Product%20Guide.pdf); [Servicing & additional Lending Policies excerpt](https://www.pepperbroker.com.au/content/dam/aubroker/broker-au-documents-nc/home-loans/product-guide/sectioned/Servicing%20and%20additional%20HOME%20LOAN%20Lending%20Policies%20(pages%2012-14).pdf); [Lending criteria](https://www.peppermoney.com.au/resources/pepper-lending-criteria) | PDF + HTML | Effective-date versioned, appears reissued frequently | **Partial** — only lender researched with a genuinely public servicing/lending-policy document (income/employment verification, servicing calc rules); no HECS-specific clause confirmed |
| Resimac | Non-bank, mortgage | Home loan | [Disclosures](https://www.resimac.com.au/disclosures); [BrokerZone Forms & Brochures](https://broker.resimac.com.au/education-hub/forms-and-brochures); [Prime product specs](https://broker.resimac.com.au/-/media/Project/Resimac/Broker/Files/resimacprimeproductspecs.pdf) | PDF + HTML | Reviewed periodically, no fixed cadence found | No — underwriting/servicing policy sits behind broker portal login → Layer 3 candidate |
| Liberty Financial | Non-bank, mortgage | Home loan | **Not found** — general search kept resolving to unrelated US "Liberty Financial/Liberty Bank" entities; needs targeted follow-up directly on liberty.com.au | Unknown | Unknown | Unknown — flagged incomplete, follow-up required |
| Firstmac | Non-bank, mortgage | Home loan | [Deposit PDSs](https://www.firstmac.com.au/media/docs/Firstmac_Deposit_PDSs.pdf); [Financial Services Guide](https://www.firstmac.com.au/media/docs/financial-services-guide.pdf); [TMD](https://www.firstmac.com.au/home-loans/tmd); Residential Lending Policy (2021, third-party mirror) | PDF + HTML | No visible versioning on own site | **Partial, weak** — lending-policy doc exists but is 2021-dated, third-party-mirrored, no HECS mention → treat as indicative only |
| Latitude Financial | Non-bank, personal loan/credit card | Personal loan, credit card | [Personal Loan T&Cs](https://assets.latitudefinancial.com/brochures/au/latitudemoney/personal-loans-terms-conditions.pdf); [T&Cs library](https://www.latitudefinancial.com.au/terms-and-conditions-library/); [CreditLine Conditions of Use & Credit Guide](https://assets.latitudefinancial.com/legals/conditions-of-use/creditline-afs/cou.pdf) | PDF | Effective-date versioned, historic versions retained | No — Credit Guide is regulatory disclosure, not underwriting methodology → Layer 3 candidate |
| MoneyMe | Non-bank, personal loan (fintech) | Personal loan | [Credit Guide](https://mmestoragecdn.blob.core.windows.net/web2/v3/images/legal/credit-guide.pdf); [TMD (Secured PL)](https://cdn.moneyme.com.au/info/tmd/s1-tmd-secured-pl-v2.pdf); [Terms of Use](https://www.moneyme.com.au/terms-of-use) | PDF + HTML | No visible cadence; TMD versioned | No → Layer 3 candidate |
| Wisr | Non-bank, personal loan (fintech) | Personal loan | **Not found** — only marketing/rate pages surfaced; needs direct site crawl of wisr.com.au | HTML only, no PDF found | Unknown | Unknown — flagged incomplete, follow-up required |
| Afterpay (Block) | BNPL | BNPL | [Terms of Service](https://www.afterpay.com/en-AU/terms-of-service); [Specific Terms](https://www.afterpay.com/en-AU/specific-terms) | HTML (links out to Credit Guide/TMD, not directly resolved) | Terms updated 10 Jun 2025 tied to BNPL regulatory reform | No — standard Credit Guide disclosure expected, affordability methodology not published → Layer 3 candidate |
| Zip Co | BNPL | BNPL, line of credit | [Zip Pay T&Cs](https://zip.co/files/au/zip-pay-terms-and-conditions.pdf); [Zip Plus T&Cs](https://zip.co/files/au/zip-plus-terms-and-conditions.pdf); [Zip Money/Line of Credit T&Cs](https://zip.co/files/au/zip-money-terms-and-conditions.pdf); [Important Information hub](https://zip.co/au/page/important-information) | PDF + HTML | Versioned by date in filename, reissued on regulatory/product change | No → Layer 3 candidate |
| Federal Register of Legislation — Family Law Act 1975 | Government/legislation | N/A | [legislation.gov.au/C2004A00275/latest](https://www.legislation.gov.au/C2004A00275/latest) | HTML + PDF/DOCX compilations | Updated on each compilation/amendment, event-driven | N/A |
| Federal Register of Legislation — Corporations Act 2001 | Government/legislation | N/A | `https://www.legislation.gov.au/C2001A00103/latest` — **inferred, not fetch-confirmed; verify before use** | HTML + PDF | Updated on each compilation/amendment | N/A |
| Federal Register of Legislation — National Consumer Credit Protection Act 2009 | Government/legislation | N/A | [legislation.gov.au/C2009A00134/latest](https://www.legislation.gov.au/C2009A00134/latest) | HTML + PDF | Updated on each compilation/amendment | N/A |
| ATO / Study Assist — HECS-HELP indexation & repayment thresholds | Government | N/A | [ATO — Study/training loan rates & thresholds](https://www.ato.gov.au/tax-rates-and-codes/study-and-training-support-loans-rates-and-repayment-thresholds); [Study Assist — Loan repayments](https://www.studyassist.gov.au/managing-and-repaying-your-loan/loan-repayments); [Dept of Education — HELP indexation](https://www.education.gov.au/higher-education-loan-program/help-students/help-indexation-and-debt-reduction) | HTML | Annual (new thresholds each July; indexation figure each 1 June) | N/A |
| Services Australia — Centrelink debts/thresholds | Government | N/A | [servicesaustralia.gov.au/centrelink-debts-and-overpayments](https://www.servicesaustralia.gov.au/centrelink-debts-and-overpayments) | HTML | Indexed twice yearly (Mar/Sep); waiver-threshold changes ad hoc | N/A |
| ASIC — RG 209 Responsible Lending / HELP debt guidance | Government/regulatory | N/A | [RG 209 — Credit licensing: Responsible lending conduct](https://www.asic.gov.au/regulatory-resources/find-a-document/regulatory-guides/rg-209-credit-licensing-responsible-lending-conduct/); [ASIC news — updated HELP debt guidance](https://www.asic.gov.au/about-asic/news-centre/news-items/asic-has-updated-guidance-to-clarify-treatment-of-student-loan-commitments-by-banks-and-lenders/); [CP 309 — Update to RG 209](https://www.asic.gov.au/regulatory-resources/find-a-document/consultations/cp-309-update-to-rg-209-credit-licensing-responsible-lending-conduct) | HTML + PDF | Updated via formal consultation process, infrequent but high-impact | N/A — but this is the most authoritative *public* document on HECS/HELP-in-serviceability treatment across the whole project; individual bank policies implement this regulator-level guidance. Recommend anchoring the HECS serviceability rule's highest-tier source here (`regulator_guidance`) rather than any single bank page. |

## Follow-ups before this feeds extraction

1. **Liberty Financial** and **Wisr** — no usable T&Cs/policy URL found; needs a targeted site-specific crawl, not general search.
2. **Corporations Act 2001** URL is inferred from the ID pattern, not fetch-confirmed — verify the exact `/latest` identifier on legislation.gov.au directly.
3. **Firstmac's** lending-policy doc is 2021-dated and third-party-mirrored — do not treat as current/authoritative without re-verification.
4. Across every lender researched, none publish a full serviceability methodology publicly. That confirms CONTEXT.md's premise: this data has to come from Layer 3 (broker/press-sourced, `broker_sourced` or `regulator_guidance` tier), not from scraping any lender's own PDS/T&Cs.
