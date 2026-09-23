"""Unit tests for the deterministic attendance rule (app/rules/attendance.py)."""
from __future__ import annotations

from decimal import Decimal

from app.rules.attendance import compute_attendance
from app.schemas.academic import AttendanceRuleStatus


def test_demo_student_os_attendance_matches_spec_example() -> None:
    """34/50 against a 75% requirement: 68.0%, not eligible, 14 classes needed."""
    result = compute_attendance(34, 50, Decimal("75"))
    assert result.status == AttendanceRuleStatus.OK
    assert result.current_percentage == Decimal("68.0")
    assert result.eligible_now is False
    assert result.classes_needed_to_reach_threshold == 14
    assert result.threshold_reachable is True
    # Verify the closed-form answer actually clears the threshold.
    attended, conducted = 34 + 14, 50 + 14
    assert Decimal(attended) / Decimal(conducted) >= Decimal("0.75")
    assert Decimal(34 + 13) / Decimal(50 + 13) < Decimal("0.75")


def test_already_eligible_reports_zero_classes_needed_and_a_margin() -> None:
    result = compute_attendance(47, 50, Decimal("75"))
    assert result.eligible_now is True
    assert result.classes_needed_to_reach_threshold == 0
    assert result.maximum_additional_absences_allowed == 12
    # One more than the reported margin must actually drop below threshold.
    y = result.maximum_additional_absences_allowed
    assert Decimal(47) / Decimal(50 + y) >= Decimal("0.75")
    assert Decimal(47) / Decimal(50 + y + 1) < Decimal("0.75")


def test_exactly_at_threshold_is_eligible() -> None:
    """30/40 == exactly 75% -- must be treated as eligible, not just-under."""
    result = compute_attendance(30, 40, Decimal("75"))
    assert result.current_percentage == Decimal("75.0")
    assert result.eligible_now is True
    assert result.classes_needed_to_reach_threshold == 0


def test_zero_classes_conducted_is_undefined_not_zero_percent() -> None:
    result = compute_attendance(0, 0, Decimal("75"))
    assert result.status == AttendanceRuleStatus.NO_CLASSES_CONDUCTED
    assert result.current_percentage is None
    assert result.eligible_now is None
    assert result.classes_needed_to_reach_threshold is None


def test_attended_greater_than_conducted_is_invalid() -> None:
    result = compute_attendance(10, 5, Decimal("75"))
    assert result.status == AttendanceRuleStatus.INVALID_INPUT


def test_negative_counts_are_invalid() -> None:
    assert compute_attendance(-1, 10, Decimal("75")).status == AttendanceRuleStatus.INVALID_INPUT
    assert compute_attendance(1, -10, Decimal("75")).status == AttendanceRuleStatus.INVALID_INPUT


def test_required_percentage_out_of_range_is_invalid() -> None:
    assert compute_attendance(10, 20, Decimal("-5")).status == AttendanceRuleStatus.INVALID_INPUT
    assert compute_attendance(10, 20, Decimal("150")).status == AttendanceRuleStatus.INVALID_INPUT


def test_zero_percent_attendance_computes_a_finite_recovery_path() -> None:
    result = compute_attendance(0, 50, Decimal("75"))
    assert result.current_percentage == Decimal("0.0")
    assert result.eligible_now is False
    assert result.classes_needed_to_reach_threshold == 150
    attended, conducted = 0 + 150, 50 + 150
    assert Decimal(attended) / Decimal(conducted) >= Decimal("0.75")


def test_hundred_percent_attendance_is_eligible_with_a_margin() -> None:
    result = compute_attendance(50, 50, Decimal("75"))
    assert result.current_percentage == Decimal("100.0")
    assert result.eligible_now is True
    assert result.maximum_additional_absences_allowed == 16


def test_hundred_percent_requirement_unreachable_once_a_class_is_missed() -> None:
    """If the policy demanded literal 100% attendance, a single miss can never be recovered."""
    result = compute_attendance(49, 50, Decimal("100"))
    assert result.eligible_now is False
    assert result.threshold_reachable is False
    assert result.classes_needed_to_reach_threshold is None


def test_hundred_percent_requirement_already_met_is_eligible() -> None:
    result = compute_attendance(50, 50, Decimal("100"))
    assert result.eligible_now is True
    assert result.classes_needed_to_reach_threshold == 0
