"""
Arithmetic plausibility check for claimed Layer 3 serviceability-impact
scenarios -- CONTEXT.md 9d: "where a claim includes a specific numeric
consequence (like the CBA $490k->$600k example), sanity-check it against
standard serviceability-calculator assumptions ... a mechanically implausible
number is a reason to downgrade confidence even if the source seems credible."

This is DELIBERATELY not a reproduction of any real lender's calculator (those
aren't public -- that's the whole premise of this project). It's a coarse
reasonableness check built on the standard mortgage-serviceability annuity
formula and generic assumptions, meant to catch numbers that are wildly off
from what the *described mechanism* could plausibly cause -- not to validate
the exact figure. It is a SUPPORTING SIGNAL ONLY: nothing in this module gates
promotion or rejects a candidate. Its result is meant to be attached to a
candidate/rule's stored metadata for a human reviewer to weigh alongside
everything else (source count, recency, broker verification, etc.).
"""

from dataclasses import dataclass, field

# Standard assumptions, deliberately generic (not lender-specific) -- overridable
# per call since actual base rates/terms vary, but these defaults reflect a
# fairly typical current owner-occupier variable rate and a standard loan term.
DEFAULT_BASE_INTEREST_RATE_PCT = 6.0
DEFAULT_LOAN_TERM_YEARS = 30
DEFAULT_STANDARD_BUFFER_PCT = 3.0  # APRA's traditional serviceability buffer (CONTEXT.md 4f)

# How far claimed can diverge from expected before being flagged. Generous on
# purpose -- assumption uncertainty (actual base rate, term, buffer, borrower
# specifics) is large, and the goal is to catch numbers that are mechanically
# absurd, not to nitpick a claim that's merely imprecise.
ABSOLUTE_TOLERANCE_PP = 15.0  # percentage points
RATIO_TOLERANCE = 2.5  # claimed can be up to this many times the expected swing


@dataclass(frozen=True)
class PlausibilityCheckResult:
    plausible: bool | None  # None = couldn't be assessed (missing required scenario info)
    claimed_swing_pct: float
    expected_swing_pct: float | None
    explanation: str
    assumptions: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Plain-dict form for storing in a JSON column (CandidateRule/ProductionRule.plausibility_check)."""
        return {
            "plausible": self.plausible,
            "claimed_swing_pct": round(self.claimed_swing_pct, 2),
            "expected_swing_pct": round(self.expected_swing_pct, 2) if self.expected_swing_pct is not None else None,
            "explanation": self.explanation,
            "assumptions": self.assumptions,
        }


def _annuity_factor(monthly_rate: float, n_payments: int) -> float:
    """(1 - (1+r)^-n) / r -- present value of a $1/period annuity."""
    return (1 - (1 + monthly_rate) ** -n_payments) / monthly_rate


def _implied_monthly_capacity(borrowing_capacity_aud: float, monthly_rate: float, n_payments: int) -> float:
    """Invert the standard repayment formula: given a borrowing capacity figure at a
    given assessment rate/term, what monthly repayment capacity would produce it?"""
    return borrowing_capacity_aud * monthly_rate / (1 - (1 + monthly_rate) ** -n_payments)


def check_serviceability_swing_plausibility(
    *,
    lender: str,
    debt_type: str,
    before_borrowing_capacity_aud: float,
    after_borrowing_capacity_aud: float,
    policy_change: dict,
    base_interest_rate_pct: float = DEFAULT_BASE_INTEREST_RATE_PCT,
    loan_term_years: int = DEFAULT_LOAN_TERM_YEARS,
) -> PlausibilityCheckResult:
    """
    Check whether a claimed before/after borrowing-capacity swing is mechanically
    plausible under standard Australian serviceability-calculator assumptions.

    `policy_change` describes the mechanism the claim attributes the swing to.
    Two mechanisms are supported:

    - {"mechanism": "buffer_reduced", "buffer_before_pct": 3.0, "buffer_after_pct": 1.0}
      The claim is that a serviceability buffer applied to the loan changed. Holds
      the borrower's implied monthly repayment capacity constant (derived from
      `before_borrowing_capacity_aud` at the OLD assessment rate) and recomputes
      what borrowing capacity that same monthly capacity would produce at the NEW
      assessment rate -- this is what CONTEXT.md's CBA $490k->$600k example
      describes (crossing the 5-year HECS threshold drops the buffer from 3% to 1%).

    - {"mechanism": "debt_excluded", "monthly_repayment_freed_aud": 350}
      The claim is that an existing debt's assumed monthly repayment was removed
      from serviceability entirely (e.g. HECS excluded once under 1 year
      remaining). Adds that amount to the implied monthly capacity (using
      DEFAULT_STANDARD_BUFFER_PCT unless policy_change also states a buffer) and
      recomputes borrowing capacity at the same assessment rate.

    Returns plausible=None (not True or False) if `policy_change` doesn't supply
    what's needed to compute an expected figure -- this function never guesses a
    verdict from insufficient information, matching this project's broader
    "don't guess" principle.

    This is a coarse sanity check, not lender-calculator reproduction (no real
    lender publishes theirs -- that's the whole premise of CONTEXT.md section 2).
    Deliberately generous tolerance (see module constants) since actual base
    rate/term/borrower specifics are unknown; it exists to catch numbers that are
    mechanically absurd relative to the described mechanism, not to fact-check
    the precise figure.
    """
    assumptions = {
        "base_interest_rate_pct": base_interest_rate_pct,
        "loan_term_years": loan_term_years,
        "tolerance_absolute_pp": ABSOLUTE_TOLERANCE_PP,
        "tolerance_ratio": RATIO_TOLERANCE,
    }

    if before_borrowing_capacity_aud <= 0:
        return PlausibilityCheckResult(
            plausible=None,
            claimed_swing_pct=0.0,
            expected_swing_pct=None,
            explanation="before_borrowing_capacity_aud must be positive -- cannot compute a swing.",
            assumptions=assumptions,
        )

    claimed_swing_pct = (after_borrowing_capacity_aud - before_borrowing_capacity_aud) / before_borrowing_capacity_aud * 100
    n_payments = loan_term_years * 12
    mechanism = policy_change.get("mechanism")

    if mechanism == "buffer_reduced":
        buffer_before = policy_change.get("buffer_before_pct")
        buffer_after = policy_change.get("buffer_after_pct")
        if buffer_before is None or buffer_after is None:
            return PlausibilityCheckResult(
                plausible=None,
                claimed_swing_pct=claimed_swing_pct,
                expected_swing_pct=None,
                explanation="mechanism='buffer_reduced' requires buffer_before_pct and buffer_after_pct.",
                assumptions=assumptions,
            )

        rate_before = (base_interest_rate_pct + buffer_before) / 100 / 12
        rate_after = (base_interest_rate_pct + buffer_after) / 100 / 12

        implied_monthly_capacity = _implied_monthly_capacity(before_borrowing_capacity_aud, rate_before, n_payments)
        expected_after = implied_monthly_capacity * _annuity_factor(rate_after, n_payments)
        expected_swing_pct = (expected_after - before_borrowing_capacity_aud) / before_borrowing_capacity_aud * 100

        assumptions.update(buffer_before_pct=buffer_before, buffer_after_pct=buffer_after)
        explanation = (
            f"mechanism=buffer_reduced: holding the implied monthly repayment capacity "
            f"(~${implied_monthly_capacity:,.0f}/mo, derived from the claimed before-figure "
            f"at {base_interest_rate_pct + buffer_before:.1f}% assessment rate) constant, "
            f"reducing the assessment rate to {base_interest_rate_pct + buffer_after:.1f}% "
            f"(base {base_interest_rate_pct:.1f}% + {buffer_after:.1f}% buffer) over "
            f"{loan_term_years} years would mechanically produce a borrowing capacity of "
            f"~${expected_after:,.0f} ({expected_swing_pct:+.1f}%), vs. the claimed "
            f"${after_borrowing_capacity_aud:,.0f} ({claimed_swing_pct:+.1f}%)."
        )

    elif mechanism == "debt_excluded":
        monthly_freed = policy_change.get("monthly_repayment_freed_aud")
        if monthly_freed is None:
            return PlausibilityCheckResult(
                plausible=None,
                claimed_swing_pct=claimed_swing_pct,
                expected_swing_pct=None,
                explanation="mechanism='debt_excluded' requires monthly_repayment_freed_aud.",
                assumptions=assumptions,
            )

        buffer_pct = policy_change.get("buffer_pct", DEFAULT_STANDARD_BUFFER_PCT)
        rate = (base_interest_rate_pct + buffer_pct) / 100 / 12

        implied_monthly_capacity = _implied_monthly_capacity(before_borrowing_capacity_aud, rate, n_payments)
        new_monthly_capacity = implied_monthly_capacity + monthly_freed
        expected_after = new_monthly_capacity * _annuity_factor(rate, n_payments)
        expected_swing_pct = (expected_after - before_borrowing_capacity_aud) / before_borrowing_capacity_aud * 100

        assumptions.update(buffer_pct=buffer_pct, monthly_repayment_freed_aud=monthly_freed)
        explanation = (
            f"mechanism=debt_excluded: freeing ${monthly_freed:,.0f}/mo of previously-committed "
            f"repayment capacity (assessment rate held at {base_interest_rate_pct + buffer_pct:.1f}%) "
            f"would mechanically produce a borrowing capacity of ~${expected_after:,.0f} "
            f"({expected_swing_pct:+.1f}%), vs. the claimed ${after_borrowing_capacity_aud:,.0f} "
            f"({claimed_swing_pct:+.1f}%)."
        )

    else:
        return PlausibilityCheckResult(
            plausible=None,
            claimed_swing_pct=claimed_swing_pct,
            expected_swing_pct=None,
            explanation=(
                f"Unsupported or missing policy_change['mechanism'] ({mechanism!r}) -- "
                f"expected 'buffer_reduced' or 'debt_excluded'. Cannot compute an expected swing."
            ),
            assumptions=assumptions,
        )

    divergence = abs(claimed_swing_pct - expected_swing_pct)
    tolerance = max(ABSOLUTE_TOLERANCE_PP, RATIO_TOLERANCE * abs(expected_swing_pct))
    plausible = divergence <= tolerance

    return PlausibilityCheckResult(
        plausible=plausible,
        claimed_swing_pct=claimed_swing_pct,
        expected_swing_pct=expected_swing_pct,
        explanation=explanation
        + (
            f" Divergence {divergence:.1f}pp is within tolerance ({tolerance:.1f}pp) -- plausible."
            if plausible
            else f" Divergence {divergence:.1f}pp EXCEEDS tolerance ({tolerance:.1f}pp) -- flagged as implausible."
        ),
        assumptions=assumptions,
    )
