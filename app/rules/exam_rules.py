"""Exam rules (AgentOS V2 Phase 4): plain, deterministic Python over plain values. No LLM, no database.

Boundaries:

* The exam ends at ``scheduled_at + duration``. It can be started from ``START_EARLY`` before its scheduled
  time until its end; it is *finished* once COMPLETED, or once its end has passed (``now >= end``).
* Attendance PRESENT / ABSENT is recorded only once the exam is IN_PROGRESS or COMPLETED; EXEMPT also before.
  MAKEUP_COMPLETED is recorded only for a student marked ABSENT, and is final.
* A target is *resolved* when PRESENT, EXEMPT or MAKEUP_COMPLETED. Success = finished AND every target resolved.
* The terminal deadline is ``makeup_deadline_at`` when set, else ``end + terminal_after_end``. The Guardian's last
  checkpoint is that deadline + ``SETTLE``; unresolved targets then end the mission FAILED (never a success).
* Reminders are allowed from ``start - max(reminder_hours)`` until the start; absence follow-ups from
  ``start + start_grace`` until the terminal deadline, only for a confirmed ABSENT mark. Contact limits
  (active request, attempts, cooldown, quiet hours) come from ``app.rules.followup_policy``, per purpose.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, List, Optional

from app.db.models.academic import ExamStatus
from app.db.models.exam import ExamAttendanceStatus
from app.rules.followup_policy import FollowupPolicy, check_contact_limits

SETTLE = timedelta(seconds=60)
START_EARLY = timedelta(minutes=15)
DEFAULT_REMINDER_HOURS = (24.0, 2.0, 0.5)
MIN_DURATION, MAX_DURATION = 5, 600  # minutes
REASON_CODES = ("MEDICAL", "OFFICIAL_DUTY", "INSTITUTION_EXEMPTION", "FAMILY_EMERGENCY", "OTHER_APPROVED")
RESOLVED = frozenset({ExamAttendanceStatus.PRESENT, ExamAttendanceStatus.EXEMPT, ExamAttendanceStatus.MAKEUP_COMPLETED})
PURPOSE_REMINDER, PURPOSE_ABSENCE = "exam_reminder", "absence_followup"


@dataclass(frozen=True)
class ExamPolicy:
    reminder: FollowupPolicy = field(default_factory=lambda: FollowupPolicy(cooldown=timedelta(minutes=20), max_attempts=3))
    absence: FollowupPolicy = field(default_factory=FollowupPolicy)
    reminder_hours: tuple = DEFAULT_REMINDER_HOURS
    start_grace: timedelta = timedelta(minutes=15)
    terminal_after_end: timedelta = timedelta(days=7)

    def __post_init__(self) -> None:
        if not self.reminder_hours or any(h <= 0 for h in self.reminder_hours):
            raise ValueError("reminder hours must be positive")
        if self.start_grace < timedelta(0) or self.terminal_after_end <= timedelta(0):
            raise ValueError("start_grace must be >= 0 and terminal_after_end > 0")


# --- Times ------------------------------------------------------------------------------------------------------


def exam_end(start: datetime, duration_minutes: int) -> datetime:
    return start + timedelta(minutes=duration_minutes)


def terminal_deadline(end: datetime, makeup_deadline_at: Optional[datetime], policy: ExamPolicy) -> datetime:
    return makeup_deadline_at if makeup_deadline_at is not None else end + policy.terminal_after_end


def reminder_window_opens(start: datetime, policy: ExamPolicy) -> datetime:
    return start - timedelta(hours=max(policy.reminder_hours))


def grace_ends(start: datetime, policy: ExamPolicy) -> datetime:
    return start + policy.start_grace


def finished(status: Optional[ExamStatus], end: datetime, now: datetime) -> bool:
    if status == ExamStatus.COMPLETED:
        return True
    return status in (ExamStatus.SCHEDULED, ExamStatus.IN_PROGRESS) and now >= end


def is_resolved(status: Optional[ExamAttendanceStatus]) -> bool:
    return status in RESOLVED


def all_resolved(target_count: int, resolved_count: int) -> bool:
    """Success needs at least one target and every one of them resolved."""
    return target_count > 0 and resolved_count >= target_count


def checkpoints(published_at: datetime, start: datetime, end: datetime, terminal: datetime,
                policy: ExamPolicy) -> List[datetime]:
    """The instants the Guardian reasons at: scheduling, each reminder ``start - h`` after scheduling, the end of
    the start grace period, the end settle point, absence re-checks one absence cooldown apart (at most
    ``absence.max_attempts``, before the terminal deadline), and the terminal settle point. Sorted, unique."""
    points = {published_at, grace_ends(start, policy), end + SETTLE, terminal + SETTLE}
    for hours in policy.reminder_hours:
        point = start - timedelta(hours=hours)
        if published_at < point < start:
            points.add(point)
    if policy.absence.cooldown > timedelta(0):
        for k in range(1, policy.absence.max_attempts + 1):
            point = end + SETTLE + k * policy.absence.cooldown
            if point < terminal:
                points.add(point)
    return sorted(p for p in points if p >= published_at)


def next_checkpoint(points: Iterable[datetime], now: datetime) -> Optional[datetime]:
    return next((p for p in sorted(points) if p > now), None)


# --- Lifecycle ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleDecision:
    allowed: bool
    code: str = "OK"


def can_start(status: Optional[ExamStatus], start: datetime, end: datetime, now: datetime) -> RuleDecision:
    if status != ExamStatus.SCHEDULED:
        return RuleDecision(False, "EXAM_NOT_SCHEDULED")
    if now < start - START_EARLY:
        return RuleDecision(False, "TOO_EARLY_TO_START")
    if now >= end:
        return RuleDecision(False, "EXAM_TIME_OVER")
    return RuleDecision(True)


def can_complete(status: Optional[ExamStatus]) -> RuleDecision:
    return RuleDecision(True) if status == ExamStatus.IN_PROGRESS else RuleDecision(False, "EXAM_NOT_IN_PROGRESS")


def can_cancel(status: Optional[ExamStatus]) -> RuleDecision:
    ok = status in (ExamStatus.DRAFT, ExamStatus.SCHEDULED, ExamStatus.IN_PROGRESS)
    return RuleDecision(True) if ok else RuleDecision(False, "EXAM_NOT_CANCELLABLE")


def can_mark(exam_status: Optional[ExamStatus], current: Optional[ExamAttendanceStatus],
             new: ExamAttendanceStatus) -> RuleDecision:
    """May this attendance value be recorded now? (Recording the current value again is an allowed no-op.)"""
    if exam_status not in (ExamStatus.SCHEDULED, ExamStatus.IN_PROGRESS, ExamStatus.COMPLETED):
        return RuleDecision(False, "EXAM_NOT_ACTIVE")
    if new == current:
        return RuleDecision(True, "UNCHANGED")
    if current == ExamAttendanceStatus.MAKEUP_COMPLETED:
        return RuleDecision(False, "MAKEUP_ALREADY_RECORDED")
    if new == ExamAttendanceStatus.MAKEUP_COMPLETED:
        return RuleDecision(True) if current == ExamAttendanceStatus.ABSENT else RuleDecision(False, "MAKEUP_REQUIRES_ABSENCE")
    if new in (ExamAttendanceStatus.PRESENT, ExamAttendanceStatus.ABSENT) and exam_status == ExamStatus.SCHEDULED:
        return RuleDecision(False, "EXAM_NOT_STARTED")
    return RuleDecision(True)


# --- Follow-up policy -----------------------------------------------------------------------------------------------


class ExamFollowupRefusal(str, enum.Enum):
    EXAM_NOT_ACTIVE = "EXAM_NOT_ACTIVE"
    NOT_A_TARGET = "NOT_A_TARGET"
    EXAM_ALREADY_STARTED = "EXAM_ALREADY_STARTED"
    REMINDER_WINDOW_NOT_OPEN = "REMINDER_WINDOW_NOT_OPEN"
    STUDENT_EXEMPT = "STUDENT_EXEMPT"
    STUDENT_PRESENT = "STUDENT_PRESENT"
    MAKEUP_COMPLETED = "MAKEUP_COMPLETED"
    ABSENCE_NOT_CONFIRMED = "ABSENCE_NOT_CONFIRMED"
    GRACE_PERIOD_NOT_OVER = "GRACE_PERIOD_NOT_OVER"
    TERMINAL_DEADLINE_PASSED = "TERMINAL_DEADLINE_PASSED"
    UNKNOWN_PURPOSE = "UNKNOWN_PURPOSE"


@dataclass(frozen=True)
class ExamFollowupDecision:
    allowed: bool
    reason: str
    attempt_number: Optional[int] = None
    not_before: Optional[datetime] = None
    urgency: Optional[str] = None


def evaluate_exam_followup(
    *, now: datetime, purpose: str, exam_status: Optional[ExamStatus], start: datetime, terminal: datetime,
    is_target: bool, attendance: Optional[ExamAttendanceStatus], active_followup: bool, attempts_so_far: int,
    last_requested_at: Optional[datetime], policy: ExamPolicy,
) -> ExamFollowupDecision:
    """May the Guardian request contact with this student about this exam now? The first refusal wins."""
    def refuse(reason: ExamFollowupRefusal) -> ExamFollowupDecision:
        return ExamFollowupDecision(False, reason.value)

    if exam_status not in (ExamStatus.SCHEDULED, ExamStatus.IN_PROGRESS, ExamStatus.COMPLETED):
        return refuse(ExamFollowupRefusal.EXAM_NOT_ACTIVE)
    if not is_target:
        return refuse(ExamFollowupRefusal.NOT_A_TARGET)
    if attendance == ExamAttendanceStatus.EXEMPT:
        return refuse(ExamFollowupRefusal.STUDENT_EXEMPT)
    if purpose == PURPOSE_REMINDER:
        if exam_status != ExamStatus.SCHEDULED or now >= start:
            return refuse(ExamFollowupRefusal.EXAM_ALREADY_STARTED)
        if now < reminder_window_opens(start, policy):
            return refuse(ExamFollowupRefusal.REMINDER_WINDOW_NOT_OPEN)
        limits, hard_stop, urgency = policy.reminder, start, ("high" if start - now <= timedelta(hours=2) else "normal")
    elif purpose == PURPOSE_ABSENCE:
        if attendance == ExamAttendanceStatus.PRESENT:
            return refuse(ExamFollowupRefusal.STUDENT_PRESENT)
        if attendance == ExamAttendanceStatus.MAKEUP_COMPLETED:
            return refuse(ExamFollowupRefusal.MAKEUP_COMPLETED)
        if attendance != ExamAttendanceStatus.ABSENT:
            return refuse(ExamFollowupRefusal.ABSENCE_NOT_CONFIRMED)
        if now < grace_ends(start, policy):
            return refuse(ExamFollowupRefusal.GRACE_PERIOD_NOT_OVER)
        if now >= terminal:
            return refuse(ExamFollowupRefusal.TERMINAL_DEADLINE_PASSED)
        limits, hard_stop, urgency = policy.absence, terminal, "high"
    else:
        return refuse(ExamFollowupRefusal.UNKNOWN_PURPOSE)
    decision = check_contact_limits(now=now, active_followup=active_followup, attempts_so_far=attempts_so_far,
                                    last_requested_at=last_requested_at, policy=limits, hard_stop=hard_stop)
    if not decision.allowed:
        return ExamFollowupDecision(False, decision.reason)
    return ExamFollowupDecision(True, decision.reason, attempt_number=decision.attempt_number,
                                not_before=decision.not_before, urgency=urgency)
