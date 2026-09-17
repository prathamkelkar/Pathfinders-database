"""
Read-only query interface against `production_rules` (section 7). Deterministic,
no live fetching, no LLM calls -- just filtering rows already in the database.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import ProductionRule


def _conditions_match(conditions: dict, years_remaining: float) -> bool:
    years_min = conditions.get("years_remaining_min")
    years_max = conditions.get("years_remaining_max")

    if years_min is not None and years_remaining < years_min:
        return False
    if years_max is not None and years_remaining >= years_max:
        return False
    return True


def get_serviceability_treatment(
    session: Session, *, lender: str, debt_type: str, years_remaining: float
) -> dict:
    """
    Return the `effect` dict of the production rule whose `conditions` match the given
    years_remaining, for the given lender + debt_type.

    Raises LookupError if no matching rule is found.
    """
    rules = session.scalars(
        select(ProductionRule).where(
            ProductionRule.lender == lender,
            ProductionRule.debt_type == debt_type,
        )
    ).all()

    for rule in rules:
        if _conditions_match(rule.conditions, years_remaining):
            return rule.effect

    raise LookupError(
        f"No production rule found for lender={lender!r}, debt_type={debt_type!r}, "
        f"years_remaining={years_remaining!r}"
    )
