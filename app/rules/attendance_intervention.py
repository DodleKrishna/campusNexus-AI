"""Attendance intervention rules (AgentOS V2 Phase 4): plain, deterministic Python. No LLM, no database.

* An absence is *confirmed* for one student and one class session only when every check passes, in order:
  the class was actually held (started; ACTIVE or CLOSED -- never SCHEDULED or CANCELLED), the absence grace
  period after the actual start has elapsed (``now >= started + grace``), the student has no attending mark
  (an ABSENT mark; or no mark at all once the roll is being taken -- at least one student of the session is
  already marked attending -- so a class whose roll has not started yet never floods interventions), no
  EXCUSED mark, no approved leave/OD/attendance permission covering the class, and no intervention already
  exists for that student and session.
* An open intervention is *resolved* by facts only: an attending mark (PRESENT/LATE: ATTENDANCE_CORRECTED when
  the student had been marked ABSENT, else ATTENDANCE_RECORDED), an EXCUSED mark or a staff-approved
  justification (ATTENDANCE_JUSTIFIED), or approved leave covering the class (LEAVE_APPROVED).
* The resolution window closes ``resolution_window`` after detection; the Guardian's last checkpoint is that
  instant + ``SETTLE``, after which an unresolved intervention ends FAILED (UNRESOLVED), never a success.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

from app.rules.followup_policy import FollowupPolicy, check_contact_limits

SETTLE = timedelta(seconds=60)
HELD_STATUSES = frozenset({"active", "closed"})
ATTENDING_MARKS = frozenset({"present", "late"})
JUSTIFICATION_CODES = ("MEDICAL", "OFFICIAL_DUTY", "FAMILY_EMERGENCY", "TRANSPORT_DISRUPTION", "OTHER_APPROVED")
LEAVE_REQUEST_TYPES = frozenset({"leave_request", "attendance_permission", "od_request"})
PURPOSE = "absence_check"


@dataclass(frozen=True)
class AttendancePolicy:
    contact: FollowupPolicy = field(default_factory=FollowupPolicy)
    grace: timedelta = timedelta(minutes=15)
    resolution_window: timedelta = timedelta(hours=72)

    def __post_init__(self) -> None:
        if self.grace < timedelta(0) or self.resolution_window <= timedelta(0):
            raise ValueError("grace must be >= 0 and resolution_window > 0")


def absence_check_due(started_at: Optional[datetime], now: datetime, policy: AttendancePolicy) -> bool:
    return started_at is not None and now >= started_at + policy.grace


def leave_covers(window_start: Optional[datetime], window_end: Optional[datetime], class_start: datetime,
                 class_end: datetime) -> bool:
    """[window_start, window_end) overlaps [class_start, class_end)."""
    if window_start is None or window_end is None:
        return False
    return window_start < class_end and class_start < window_end


class DetectionCode(str, enum.Enum):
    ABSENCE_CONFIRMED = "ABSENCE_CONFIRMED"
    CLASS_CANCELLED = "CLASS_CANCELLED"
    CLASS_NOT_HELD = "CLASS_NOT_HELD"
    GRACE_NOT_ELAPSED = "GRACE_NOT_ELAPSED"
    ATTENDANCE_RECORDED = "ATTENDANCE_RECORDED"
    EXCUSED = "EXCUSED"
    LEAVE_APPROVED = "LEAVE_APPROVED"
    INTERVENTION_EXISTS = "INTERVENTION_EXISTS"
    ROLL_NOT_TAKEN = "ROLL_NOT_TAKEN"


def detect_absence(*, session_status: str, started_at: Optional[datetime], now: datetime, mark: Optional[str],
                   roll_taken: bool, leave_approved: bool, has_intervention: bool,
                   policy: AttendancePolicy) -> DetectionCode:
    if session_status == "cancelled":
        return DetectionCode.CLASS_CANCELLED
    if session_status not in HELD_STATUSES or started_at is None:
        return DetectionCode.CLASS_NOT_HELD
    if not absence_check_due(started_at, now, policy):
        return DetectionCode.GRACE_NOT_ELAPSED
    if mark in ATTENDING_MARKS:
        return DetectionCode.ATTENDANCE_RECORDED
    if mark == "excused":
        return DetectionCode.EXCUSED
    if mark is None and not roll_taken:
        return DetectionCode.ROLL_NOT_TAKEN
    if leave_approved:
        return DetectionCode.LEAVE_APPROVED
    if has_intervention:
        return DetectionCode.INTERVENTION_EXISTS
    return DetectionCode.ABSENCE_CONFIRMED


def resolution(*, mark: Optional[str], detected_mark: str, justification_code: Optional[str],
               leave_approved: bool) -> Optional[str]:
    """How an open intervention is resolved right now, or None while it is still an unexplained absence."""
    if mark in ATTENDING_MARKS:
        return "ATTENDANCE_CORRECTED" if detected_mark == "absent" else "ATTENDANCE_RECORDED"
    if mark == "excused" or justification_code is not None:
        return "ATTENDANCE_JUSTIFIED"
    if leave_approved:
        return "LEAVE_APPROVED"
    return None


def resolution_deadline(detected_at: datetime, policy: AttendancePolicy) -> datetime:
    return detected_at + policy.resolution_window


def checkpoints(detected_at: datetime, policy: AttendancePolicy) -> List[datetime]:
    """Detection, one follow-up re-check per cooldown (at most ``max_attempts`` in all, inside the window), and the
    settle point after the resolution deadline. Sorted, unique."""
    deadline = resolution_deadline(detected_at, policy)
    points = {detected_at, deadline + SETTLE}
    if policy.contact.cooldown > timedelta(0):
        for k in range(1, policy.contact.max_attempts):
            point = detected_at + k * policy.contact.cooldown
            if point < deadline:
                points.add(point)
    return sorted(points)


class AttendanceFollowupRefusal(str, enum.Enum):
    INTERVENTION_NOT_OPEN = "INTERVENTION_NOT_OPEN"
    ABSENCE_RESOLVED = "ABSENCE_RESOLVED"
    RESOLUTION_WINDOW_CLOSED = "RESOLUTION_WINDOW_CLOSED"


@dataclass(frozen=True)
class AttendanceFollowupDecision:
    allowed: bool
    reason: str
    attempt_number: Optional[int] = None
    not_before: Optional[datetime] = None
    urgency: Optional[str] = None


def evaluate_attendance_followup(*, now: datetime, intervention_open: bool, resolved_by: Optional[str],
                                 deadline: datetime, active_followup: bool, attempts_so_far: int,
                                 last_requested_at: Optional[datetime], policy: AttendancePolicy) -> AttendanceFollowupDecision:
    if not intervention_open:
        return AttendanceFollowupDecision(False, AttendanceFollowupRefusal.INTERVENTION_NOT_OPEN.value)
    if resolved_by is not None:
        return AttendanceFollowupDecision(False, AttendanceFollowupRefusal.ABSENCE_RESOLVED.value)
    if now >= deadline:
        return AttendanceFollowupDecision(False, AttendanceFollowupRefusal.RESOLUTION_WINDOW_CLOSED.value)
    decision = check_contact_limits(now=now, active_followup=active_followup, attempts_so_far=attempts_so_far,
                                    last_requested_at=last_requested_at, policy=policy.contact, hard_stop=deadline)
    if not decision.allowed:
        return AttendanceFollowupDecision(False, decision.reason)
    return AttendanceFollowupDecision(True, decision.reason, attempt_number=decision.attempt_number,
                                      not_before=decision.not_before,
                                      urgency="high" if decision.attempt_number and decision.attempt_number > 1 else "normal")
