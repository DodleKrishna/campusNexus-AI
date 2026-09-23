"""Deterministic attendance calculation (CLAUDE.md: official-rule arithmetic
is plain Python over Decimal, never an LLM call, never float).

``current_percentage`` is rounded to 1 decimal place purely for display,
matching the policy text's own documented calculation method
("classes attended divided by classes conducted, ... rounded to one decimal
place"). Every *decision* (``eligible_now``, ``classes_needed_to_reach_threshold``,
``maximum_additional_absences_allowed``) is computed from the exact,
unrounded ratio via closed-form Decimal algebra -- no iteration, no
rounding-induced boundary ambiguity, so "exactly at threshold" is
unambiguous (e.g. 30/40 against a 75% requirement is exactly eligible).
"""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from app.schemas.academic import AttendanceCalculation, AttendanceRuleStatus

_ONE_DECIMAL = Decimal("0.1")
_HUNDRED = Decimal(100)


def _round_to_one_decimal(value: Decimal) -> Decimal:
    return value.quantize(_ONE_DECIMAL, rounding=ROUND_HALF_UP)


def compute_attendance(
    classes_attended: int, classes_conducted: int, required_percentage: Decimal
) -> AttendanceCalculation:
    """Compute attendance eligibility and recovery math from raw counters.

    ``required_percentage`` is a percentage value (e.g. ``Decimal("75")``),
    not a fraction -- it must come from a deterministically-extracted policy
    threshold (``app.rules.policy_threshold``), never a hardcoded default.
    """
    if (
        classes_attended < 0
        or classes_conducted < 0
        or classes_attended > classes_conducted
        or required_percentage < 0
        or required_percentage > _HUNDRED
    ):
        return AttendanceCalculation(
            status=AttendanceRuleStatus.INVALID_INPUT,
            classes_attended=classes_attended,
            classes_conducted=classes_conducted,
            required_percentage=required_percentage,
            detail=(
                "Invalid input: classes_attended/classes_conducted must be "
                "non-negative with attended <= conducted, and required_percentage "
                "must be within [0, 100]."
            ),
        )

    if classes_conducted == 0:
        return AttendanceCalculation(
            status=AttendanceRuleStatus.NO_CLASSES_CONDUCTED,
            classes_attended=classes_attended,
            classes_conducted=classes_conducted,
            required_percentage=required_percentage,
            detail="No classes have been conducted yet; attendance percentage is undefined.",
        )

    attended = Decimal(classes_attended)
    conducted = Decimal(classes_conducted)
    required_fraction = required_percentage / _HUNDRED

    exact_ratio = attended / conducted
    current_percentage = _round_to_one_decimal(exact_ratio * _HUNDRED)
    eligible_now = exact_ratio >= required_fraction

    classes_needed: int | None
    threshold_reachable: bool
    max_additional_absences: int | None = None

    if eligible_now:
        classes_needed = 0
        threshold_reachable = True
        if required_fraction > 0:
            bound = attended / required_fraction - conducted
            y = int(bound.to_integral_value(rounding=ROUND_FLOOR))
            max_additional_absences = max(y, 0)
    elif required_fraction >= 1:
        # attended < conducted (else eligible_now would be True), so the gap
        # conducted - attended never closes for any finite number of future
        # classes -- 100% is provably unreachable.
        classes_needed = None
        threshold_reachable = False
    else:
        denom = Decimal(1) - required_fraction
        numer = required_fraction * conducted - attended
        raw_x = numer / denom
        x = int(raw_x.to_integral_value(rounding=ROUND_CEILING))
        classes_needed = max(x, 0)
        threshold_reachable = True

    return AttendanceCalculation(
        status=AttendanceRuleStatus.OK,
        classes_attended=classes_attended,
        classes_conducted=classes_conducted,
        required_percentage=required_percentage,
        current_percentage=current_percentage,
        eligible_now=eligible_now,
        classes_needed_to_reach_threshold=classes_needed,
        threshold_reachable=threshold_reachable,
        maximum_additional_absences_allowed=max_additional_absences,
    )
