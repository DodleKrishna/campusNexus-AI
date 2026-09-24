"""Deterministic attendance standing and class-slot status (Phase 15).

``attendance_standing`` turns an ``AttendanceCalculation`` (from
``compute_attendance``) into the label the student sees:

* BELOW_REQUIREMENT -- not eligible now (the policy threshold decides this).
* AT_RISK -- eligible now, but at most ``AT_RISK_MAX_ABSENCES`` more absences
  would drop the course below the threshold. This is an advisory display
  margin, not a policy rule; eligibility itself is only ``eligible_now``.
* GOOD -- eligible with more room than that.
* UNKNOWN -- no threshold or no classes conducted yet.

``slot_status`` says whether a timetable slot is UPCOMING, NOW or COMPLETED
at a given local wall-clock time.
"""
from __future__ import annotations

from datetime import time
from enum import Enum
from typing import Optional

from app.schemas.academic import AttendanceCalculation, AttendanceRuleStatus

AT_RISK_MAX_ABSENCES = 2


class AttendanceStanding(str, Enum):
    GOOD = "good"
    AT_RISK = "at_risk"
    BELOW_REQUIREMENT = "below_requirement"
    UNKNOWN = "unknown"


class SlotStatus(str, Enum):
    UPCOMING = "upcoming"
    NOW = "now"
    COMPLETED = "completed"


def attendance_standing(calc: Optional[AttendanceCalculation]) -> AttendanceStanding:
    if calc is None or calc.status != AttendanceRuleStatus.OK or calc.eligible_now is None:
        return AttendanceStanding.UNKNOWN
    if not calc.eligible_now:
        return AttendanceStanding.BELOW_REQUIREMENT
    margin = calc.maximum_additional_absences_allowed
    if margin is not None and margin <= AT_RISK_MAX_ABSENCES:
        return AttendanceStanding.AT_RISK
    return AttendanceStanding.GOOD


def _parse(value: str) -> time:
    hours, minutes = value.split(":")[:2]
    return time(int(hours), int(minutes))


def slot_status(start: str, end: str, now: time) -> SlotStatus:
    """``start``/``end`` are 'HH:MM' local times; the slot is NOW on [start, end)."""
    if now < _parse(start):
        return SlotStatus.UPCOMING
    if now < _parse(end):
        return SlotStatus.NOW
    return SlotStatus.COMPLETED
