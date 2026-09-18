"""
Inserts the two HELP/HECS candidate_rules from APRA's APG 223 / ARS 223.0
(ingestion/scrapers/apra_apg223.py) as hand-verified candidates, not
LLM-extracted ones.

Why hand-entered rather than run through ingestion/extraction/extract_rules.py:
this content requires getting a precise regulatory distinction right --
"ADIs MAY exclude HELP repayments from serviceability" (a permitted override,
conditional on near-term repayment, NOT a mandate) versus "HELP debt MUST be
excluded from DTI" (unconditional, mandatory) -- and this was read and verified
directly from the primary source PDFs (see apra_apg223.py's docstring for what
was actually checked). extraction_prompt_version is left None on both
candidates for the same reason ingestion/layer3_intake.py does: no LLM prompt
produced these, so tagging one would misrepresent their provenance.

These are still inserted into candidate_rules, not production_rules --
promotion is still a separate, human decision via
ingestion/review_candidates.py (section 6), same as every other candidate.
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import CandidateRule, Source

LENDER = "APRA"
DEBT_TYPE = "HECS_HELP"
COMMENCEMENT_DATE = date(2025, 9, 30)

SERVICEABILITY_RULE_DESCRIPTION = (
    "APG 223 (Prudential Practice Guide): APRA's baseline expectation is that ADIs continue "
    "to consider a borrower's HELP debt in serviceability assessments, since HELP repayments "
    "reduce income available to service a mortgage. However, it is a PERMITTED EXCEPTION, not "
    "a mandate, for an ADI to remove HELP repayments from a serviceability assessment where the "
    "borrower is expected to repay the HELP debt within 12 months via compulsory "
    "income-contingent repayments -- an ADI-level override APRA expects to be governed by a "
    "'prudent framework', not an industry-wide rule. This document sets the regulatory CEILING "
    "of what's permitted; it does NOT specify any individual lender's actual threshold (e.g. "
    "CBA's tacit 1-year/5-year cutoffs, CONTEXT.md section 2). Lender-specific thresholds still "
    "require separate Layer 3 sourcing per lender within this framework."
)

DTI_RULE_DESCRIPTION = (
    "ARS 223.0 (Reporting Standard): HELP debts MUST be excluded from the credit limit of all "
    "debts held by a borrower for the purpose of calculating the debt-to-income (DTI) ratio "
    "reported to APRA. This is unconditional and mandatory (unlike the serviceability treatment "
    "above, which is discretionary) -- but it governs APRA's DTI reporting metric specifically, "
    "not a lender's own internal serviceability/borrowing-capacity assessment."
)


def _get_source(session: Session, url: str) -> Source:
    source = session.scalars(select(Source).where(Source.url == url)).first()
    if source is None:
        raise LookupError(
            f"No source found for {url!r} -- run `python3 -m ingestion.scrapers.apra_apg223` first."
        )
    return source


def insert_apra_help_debt_candidates(session: Session) -> list[CandidateRule]:
    from ingestion.scrapers.apra_apg223 import DOCUMENTS

    apg223_source = _get_source(session, DOCUMENTS[0].url)
    ars223_source = _get_source(session, DOCUMENTS[1].url)

    serviceability_candidate = CandidateRule(
        lender=LENDER,
        debt_type=DEBT_TYPE,
        description=SERVICEABILITY_RULE_DESCRIPTION,
        conditions={"expected_repayment_within_months": 12},
        effect={
            "treatment": "may_exclude_from_serviceability",
            "mandatory": False,
            "basis": "ADI discretion via a prudent override framework -- an APRA-permitted "
            "exception, not an APRA-mandated rule",
            "scope": "industry-wide regulatory ceiling; does not specify any individual lender's "
            "actual threshold",
        },
        source_tier="regulator_guidance",
        legislation_status="in_force",
        commencement_date=COMMENCEMENT_DATE,
        effective_from=COMMENCEMENT_DATE,
        confidence="official_document",
        conflicting_sources=False,
        extraction_prompt_version=None,
        source_id=apg223_source.id,
    )

    dti_candidate = CandidateRule(
        lender=LENDER,
        debt_type=DEBT_TYPE,
        description=DTI_RULE_DESCRIPTION,
        conditions={},
        effect={
            "treatment": "excluded_from_dti_calculation",
            "mandatory": True,
            "scope": "DTI ratio's credit-limit-of-all-debts calculation only; does not affect "
            "serviceability assessment treatment (see the companion APG 223 rule)",
        },
        source_tier="regulator_guidance",
        legislation_status="in_force",
        commencement_date=COMMENCEMENT_DATE,
        effective_from=COMMENCEMENT_DATE,
        confidence="official_document",
        conflicting_sources=False,
        extraction_prompt_version=None,
        source_id=ars223_source.id,
    )

    session.add_all([serviceability_candidate, dti_candidate])
    session.flush()
    return [serviceability_candidate, dti_candidate]


if __name__ == "__main__":
    from db.session import get_session

    session = get_session()
    inserted = insert_apra_help_debt_candidates(session)
    session.commit()
    for c in inserted:
        print(f"candidate_rule id={c.id} debt_type={c.debt_type} effect={c.effect}")
