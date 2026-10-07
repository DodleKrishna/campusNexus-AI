"""Shared follow-up contact limits (AgentOS V2 Phase 4): plain, deterministic Python. No LLM, no database.

Used by the Assignment, Exam and Attendance Guardians. Each domain first applies its own facts
(submitted / present / resolved, deadlines, windows); then ``check_contact_limits`` applies the
limits every follow-up shares, in this order (the first refusal wins):

1. active-request suppression -- one undelivered request at a time;
2. maximum attempts;
3. cooldown since the last request (``now < last + cooldown`` refuses; exactly at the boundary is allowed);
4. quiet hours -- a request made in quiet hours is deferred to their end (``not_before``), or refused when
   that end is at or after the domain's ``hard_stop`` (e.g. the deadline or the exam start).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from typing import Optional

DEFAULT_CHECKPOINT_HOURS = (24.0, 6.0, 1.0)


@dataclass(frozen=True)
class FollowupPolicy:
    cooldown: timedelta = timedelta(hours=6)
    max_attempts: int = 3
    quiet_start: time = time(21, 0)  # campus-local; quiet hours may wrap midnight; start == end disables them
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


class LimitRefusal(str, enum.Enum):
    ACTIVE_FOLLOWUP_EXISTS = "ACTIVE_FOLLOWUP_EXISTS"
    MAX_ATTEMPTS_REACHED = "MAX_ATTEMPTS_REACHED"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    QUIET_HOURS_UNTIL_DEADLINE = "QUIET_HOURS_UNTIL_DEADLINE"


@dataclass(frozen=True)
class ContactDecision:
    allowed: bool
    reason: str  # REQUESTED / REQUESTED_AFTER_QUIET_HOURS, or a refusal code
    attempt_number: Optional[int] = None
    not_before: Optional[datetime] = None


def check_contact_limits(*, now: datetime, active_followup: bool, attempts_so_far: int,
                         last_requested_at: Optional[datetime], policy: FollowupPolicy,
                         hard_stop: Optional[datetime]) -> ContactDecision:
    def refuse(reason: LimitRefusal) -> ContactDecision:
        return ContactDecision(False, reason.value)

    if active_followup:
        return refuse(LimitRefusal.ACTIVE_FOLLOWUP_EXISTS)
    if attempts_so_far >= policy.max_attempts:
        return refuse(LimitRefusal.MAX_ATTEMPTS_REACHED)
    if last_requested_at is not None and now < last_requested_at + policy.cooldown:
        return refuse(LimitRefusal.COOLDOWN_ACTIVE)
    not_before = quiet_hours_end(now, policy) if in_quiet_hours(now, policy) else None
    if not_before is not None and hard_stop is not None and not_before > hard_stop:
        return refuse(LimitRefusal.QUIET_HOURS_UNTIL_DEADLINE)
    return ContactDecision(True, "REQUESTED_AFTER_QUIET_HOURS" if not_before else "REQUESTED",
                           attempt_number=attempts_so_far + 1, not_before=not_before)
