"""Assignment rules (AgentOS V2 Phase 3): plain, deterministic Python over plain values. No LLM, no database.

Boundaries:

* A submission is on time when ``submitted_at <= deadline_at`` (exactly at the deadline is on time); after it,
  LATE.
* The deadline has passed when ``now > deadline_at``. The Guardian's last checkpoint is
  ``deadline_at + DEADLINE_SETTLE`` so a submission made exactly at the deadline is seen before it decides.
* A follow-up is decided by ``evaluate_followup`` only: an AgentBrain may ask for one, this decides.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from typing import Iterable, List, Optional

from app.db.models.assignment import AssignmentStatus, SubmissionStatus

DEADLINE_SETTLE = timedelta(seconds=60)
DEFAULT_CHECKPOINT_HOURS = (24.0, 6.0, 1.0)


def classify_submission(submitted_at: datetime, deadline_at: datetime) -> SubmissionStatus:
    return SubmissionStatus.SUBMITTED if submitted_at <= deadline_at else SubmissionStatus.LATE


def deadline_passed(deadline_at: datetime, now: datetime) -> bool:
    return now > deadline_at


def all_targets_submitted(target_count: int, submitted_count: int) -> bool:
    """Success needs at least one target and every one of them submitted."""
    return target_count > 0 and submitted_count >= target_count


def assignment_cancelled(status: AssignmentStatus) -> bool:
    return status == AssignmentStatus.CANCELLED


# --- Wake checkpoints ------------------------------------------------------------------------------------------------


def checkpoints(published_at: datetime, deadline_at: datetime,
                hours_before: Iterable[float] = DEFAULT_CHECKPOINT_HOURS) -> List[datetime]:
    """The instants the Guardian reasons at: publication, each ``deadline - h`` that falls after publication
    (unavailable ones collapse away for short assignments), and the deadline settle point. Sorted, unique."""
    points = {published_at, deadline_at + DEADLINE_SETTLE}
    for hours in hours_before:
        point = deadline_at - timedelta(hours=hours)
        if published_at < point < deadline_at:
            points.add(point)
    return sorted(points)


def next_checkpoint(points: Iterable[datetime], now: datetime) -> Optional[datetime]:
    """The first checkpoint strictly after ``now`` (None when the last one has passed)."""
    return next((p for p in sorted(points) if p > now), None)


def reminder_window_opens(published_at: datetime, deadline_at: datetime,
                          hours_before: Iterable[float] = DEFAULT_CHECKPOINT_HOURS) -> datetime:
    """Follow-ups are allowed from the earliest reminder checkpoint (or publication, for short assignments)."""
    earliest = max(hours_before, default=0.0)
    return max(published_at, deadline_at - timedelta(hours=earliest))


def urgency(deadline_at: datetime, now: datetime) -> str:
    remaining = deadline_at - now
    if remaining <= timedelta(hours=6):
        return "high"
    if remaining <= timedelta(hours=24):
        return "normal"
    return "low"


# --- Follow-up policy ----------------------------------------------------------------------------------------------


class FollowupRefusal(str, enum.Enum):
    ASSIGNMENT_NOT_ACTIVE = "ASSIGNMENT_NOT_ACTIVE"
    NOT_A_TARGET = "NOT_A_TARGET"
    ALREADY_SUBMITTED = "ALREADY_SUBMITTED"
    DEADLINE_PASSED = "DEADLINE_PASSED"
    REMINDER_WINDOW_NOT_OPEN = "REMINDER_WINDOW_NOT_OPEN"
    ACTIVE_FOLLOWUP_EXISTS = "ACTIVE_FOLLOWUP_EXISTS"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    MAX_ATTEMPTS_REACHED = "MAX_ATTEMPTS_REACHED"
    QUIET_HOURS_UNTIL_DEADLINE = "QUIET_HOURS_UNTIL_DEADLINE"


@dataclass(frozen=True)
class FollowupPolicy:
    cooldown: timedelta = timedelta(hours=6)
    max_attempts: int = 3
    quiet_start: time = time(21, 0)  # campus-local; quiet hours may wrap midnight
    quiet_end: time = time(7, 0)
    timezone: Optional[tzinfo] = None  # campus timezone (None = UTC)
    checkpoint_hours: tuple = DEFAULT_CHECKPOINT_HOURS

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.cooldown < timedelta(0):
            raise ValueError("max_attempts must be >= 1 and cooldown >= 0")
        if any(h <= 0 for h in self.checkpoint_hours):
            raise ValueError("checkpoint hours must be positive")


def _local(now: datetime, policy: FollowupPolicy) -> datetime:
    return now.astimezone(policy.timezone) if policy.timezone is not None else now


def in_quiet_hours(now: datetime, policy: FollowupPolicy) -> bool:
    start, end, current = policy.quiet_start, policy.quiet_end, _local(now, policy).time()
    if start == end:
        return False
    return start <= current < end if start < end else (current >= start or current < end)


def quiet_hours_end(now: datetime, policy: FollowupPolicy) -> datetime:
    """The next instant quiet hours end, as an aware datetime in ``now``'s timezone."""
    local = _local(now, policy)
    end = local.replace(hour=policy.quiet_end.hour, minute=policy.quiet_end.minute, second=0, microsecond=0)
    if end <= local:
        end += timedelta(days=1)
    return end.astimezone(now.tzinfo)


@dataclass(frozen=True)
class FollowupDecision:
    allowed: bool
    reason: str  # REQUESTED / REQUESTED_AFTER_QUIET_HOURS, or a FollowupRefusal value
    attempt_number: Optional[int] = None
    not_before: Optional[datetime] = None
    urgency: Optional[str] = None


def evaluate_followup(
    *, now: datetime, assignment_status: AssignmentStatus, published_at: datetime, deadline_at: datetime,
    is_target: bool, has_submitted: bool, active_followup: bool, attempts_so_far: int,
    last_requested_at: Optional[datetime], policy: FollowupPolicy,
) -> FollowupDecision:
    """May the Guardian request contact with this student now? Checked in order; the first refusal wins."""
    def refuse(reason: FollowupRefusal) -> FollowupDecision:
        return FollowupDecision(False, reason.value)

    if assignment_status != AssignmentStatus.PUBLISHED:
        return refuse(FollowupRefusal.ASSIGNMENT_NOT_ACTIVE)
    if not is_target:
        return refuse(FollowupRefusal.NOT_A_TARGET)
    if has_submitted:
        return refuse(FollowupRefusal.ALREADY_SUBMITTED)
    if deadline_passed(deadline_at, now):
        return refuse(FollowupRefusal.DEADLINE_PASSED)
    if now < reminder_window_opens(published_at, deadline_at, policy.checkpoint_hours):
        return refuse(FollowupRefusal.REMINDER_WINDOW_NOT_OPEN)
    if active_followup:
        return refuse(FollowupRefusal.ACTIVE_FOLLOWUP_EXISTS)
    if attempts_so_far >= policy.max_attempts:
        return refuse(FollowupRefusal.MAX_ATTEMPTS_REACHED)
    if last_requested_at is not None and now < last_requested_at + policy.cooldown:
        return refuse(FollowupRefusal.COOLDOWN_ACTIVE)
    not_before = quiet_hours_end(now, policy) if in_quiet_hours(now, policy) else None
    if not_before is not None and not_before > deadline_at:
        return refuse(FollowupRefusal.QUIET_HOURS_UNTIL_DEADLINE)
    return FollowupDecision(True, "REQUESTED_AFTER_QUIET_HOURS" if not_before else "REQUESTED",
                            attempt_number=attempts_so_far + 1, not_before=not_before,
                            urgency=urgency(deadline_at, now))
