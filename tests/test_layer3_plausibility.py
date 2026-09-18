"""
Tests for ingestion/layer3_plausibility.py -- CONTEXT.md 9d's arithmetic
plausibility check. Pure function, no DB involved.
"""

from ingestion.layer3_plausibility import check_serviceability_swing_plausibility


def test_cba_hecs_example_is_plausible():
    """
    CONTEXT.md section 2's own worked example: a $4,000 voluntary HECS repayment
    crossing the 5-year threshold (dropping the assessment buffer from 3% to 1%)
    took borrowing capacity from $490,000 to $600,000.
    """
    result = check_serviceability_swing_plausibility(
        lender="CBA",
        debt_type="HECS_HELP",
        before_borrowing_capacity_aud=490_000,
        after_borrowing_capacity_aud=600_000,
        policy_change={"mechanism": "buffer_reduced", "buffer_before_pct": 3.0, "buffer_after_pct": 1.0},
    )

    assert result.plausible is True
    assert round(result.claimed_swing_pct, 1) == 22.4
    # expected swing from the standard model should land close to the claimed one
    assert abs(result.claimed_swing_pct - result.expected_swing_pct) < 5.0


def test_wildly_oversized_swing_is_flagged_implausible():
    """The same described mechanism (3% -> 1% buffer) cannot mechanically explain a 10x jump."""
    result = check_serviceability_swing_plausibility(
        lender="CBA",
        debt_type="HECS_HELP",
        before_borrowing_capacity_aud=490_000,
        after_borrowing_capacity_aud=4_900_000,
        policy_change={"mechanism": "buffer_reduced", "buffer_before_pct": 3.0, "buffer_after_pct": 1.0},
    )

    assert result.plausible is False
    assert result.claimed_swing_pct == 900.0


def test_a_small_understated_swing_is_still_plausible():
    """A claim smaller than the mechanism would produce isn't suspicious -- only oversized claims are."""
    result = check_serviceability_swing_plausibility(
        lender="CBA",
        debt_type="HECS_HELP",
        before_borrowing_capacity_aud=490_000,
        after_borrowing_capacity_aud=510_000,  # a modest, conservative claim
        policy_change={"mechanism": "buffer_reduced", "buffer_before_pct": 3.0, "buffer_after_pct": 1.0},
    )
    assert result.plausible is True


def test_missing_mechanism_fields_returns_none_not_a_guess():
    result = check_serviceability_swing_plausibility(
        lender="CBA",
        debt_type="HECS_HELP",
        before_borrowing_capacity_aud=490_000,
        after_borrowing_capacity_aud=600_000,
        policy_change={"mechanism": "buffer_reduced"},  # missing buffer_before_pct/buffer_after_pct
    )
    assert result.plausible is None
    assert "requires buffer_before_pct" in result.explanation


def test_unsupported_mechanism_returns_none_not_a_guess():
    result = check_serviceability_swing_plausibility(
        lender="CBA",
        debt_type="HECS_HELP",
        before_borrowing_capacity_aud=490_000,
        after_borrowing_capacity_aud=600_000,
        policy_change={"mechanism": "something_else"},
    )
    assert result.plausible is None


def test_zero_or_negative_before_capacity_returns_none():
    result = check_serviceability_swing_plausibility(
        lender="CBA",
        debt_type="HECS_HELP",
        before_borrowing_capacity_aud=0,
        after_borrowing_capacity_aud=600_000,
        policy_change={"mechanism": "buffer_reduced", "buffer_before_pct": 3.0, "buffer_after_pct": 1.0},
    )
    assert result.plausible is None


def test_debt_excluded_mechanism_plausible_case():
    """
    Freeing a modest monthly HECS repayment (~$400/mo) from serviceability should
    produce a modest, plausible borrowing-capacity increase, not a huge one.
    """
    result = check_serviceability_swing_plausibility(
        lender="CBA",
        debt_type="HECS_HELP",
        before_borrowing_capacity_aud=490_000,
        after_borrowing_capacity_aud=540_000,
        policy_change={"mechanism": "debt_excluded", "monthly_repayment_freed_aud": 400},
    )
    assert result.plausible is True


def test_debt_excluded_missing_amount_returns_none():
    result = check_serviceability_swing_plausibility(
        lender="CBA",
        debt_type="HECS_HELP",
        before_borrowing_capacity_aud=490_000,
        after_borrowing_capacity_aud=540_000,
        policy_change={"mechanism": "debt_excluded"},
    )
    assert result.plausible is None


def test_to_dict_is_json_serializable():
    import json

    result = check_serviceability_swing_plausibility(
        lender="CBA",
        debt_type="HECS_HELP",
        before_borrowing_capacity_aud=490_000,
        after_borrowing_capacity_aud=600_000,
        policy_change={"mechanism": "buffer_reduced", "buffer_before_pct": 3.0, "buffer_after_pct": 1.0},
    )
    serialized = json.dumps(result.to_dict())
    reloaded = json.loads(serialized)
    assert reloaded["plausible"] is True
