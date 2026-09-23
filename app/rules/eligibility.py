"""Deterministic exam eligibility (attendance-only).

exam_regulations.md conditions exam eligibility on both the attendance
threshold and cleared fee dues, but fee data belongs to the Campus Services
Agent, which is out of scope for this phase. This rule only evaluates the
attendance half and says so explicitly via ``ExamEligibilityResult.caveat``,
rather than silently asserting full eligibility.
"""
from __future__ import annotations

from app.schemas.academic import (
    AttendanceCalculation,
    AttendanceRuleStatus,
    ExamEligibilityResult,
    ExamEligibilityStatus,
)


def compute_exam_eligibility(attendance: AttendanceCalculation) -> ExamEligibilityResult:
    """Derive attendance-based exam eligibility from an already-computed AttendanceCalculation."""
    if attendance.status != AttendanceRuleStatus.OK or attendance.eligible_now is None:
        status = ExamEligibilityStatus.UNKNOWN
    elif attendance.eligible_now:
        status = ExamEligibilityStatus.ELIGIBLE
    else:
        status = ExamEligibilityStatus.NOT_ELIGIBLE

    return ExamEligibilityResult(status=status, attendance=attendance)
