"""Deterministic class-session rules (Phase 16).

Pure functions over statuses and timezone-aware instants -- no DB, no LLM.

* A class can be started from ``START_EARLY_MINUTES`` before its scheduled
  start until its scheduled end, on its own date only, and only from
  SCHEDULED.
* Attendance can be marked only while the session is ACTIVE.
* A session can be closed only from ACTIVE, and only once every student on
  the roster has a mark (unmarked students are never silently made absent).
* A SCHEDULED session can be cancelled; an ACTIVE or CLOSED one cannot.
* For the attendance counters (``attendance_records``), the policy formula is
  classes attended / classes conducted. A closed session counts as conducted
  for every rostered student and as attended for PRESENT and LATE marks.
  EXCUSED is recorded but is not attended; any condonation stays with the
  Academic Office, as the attendance policy says.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable, Optional

START_EARLY_MINUTES = 15
ATTENDED_MARKS = frozenset({"present", "late"})
MARK_STATUSES = ("present", "absent", "late", "excused")


class SessionState(str, Enum):
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    CLOSED = "closed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RuleDecision:
    allowed: bool
    reason: str = ""


def _aware(value: datetime) -> None:
    if value.tzinfo is None:
        raise ValueError("class-session rules require timezone-aware datetimes")


def start_window_opens(scheduled_start: datetime) -> datetime:
    return scheduled_start - timedelta(minutes=START_EARLY_MINUTES)


def can_start(status: str, scheduled_start: datetime, scheduled_end: datetime, now: datetime) -> RuleDecision:
    for value in (scheduled_start, scheduled_end, now):
        _aware(value)
    if status != SessionState.SCHEDULED.value:
        return RuleDecision(False, f"This class is already {status}.")
    if now < start_window_opens(scheduled_start):
        return RuleDecision(False, f"A class can be started at most {START_EARLY_MINUTES} minutes before its scheduled time.")
    if now >= scheduled_end:
        return RuleDecision(False, "This class's scheduled time has already ended.")
    return RuleDecision(True)


def can_mark(status: str) -> RuleDecision:
    if status != SessionState.ACTIVE.value:
        return RuleDecision(False, "Attendance can only be marked while the class is in session.")
    return RuleDecision(True)


def can_close(status: str, unmarked_count: int) -> RuleDecision:
    if status != SessionState.ACTIVE.value:
        return RuleDecision(False, f"Only a class in session can be closed (this one is {status}).")
    if unmarked_count > 0:
        noun = "student has" if unmarked_count == 1 else "students have"
        return RuleDecision(False, f"{unmarked_count} {noun} not been marked yet.")
    return RuleDecision(True)


def can_cancel(status: str) -> RuleDecision:
    if status != SessionState.SCHEDULED.value:
        return RuleDecision(False, f"Only a class that has not started can be cancelled (this one is {status}).")
    return RuleDecision(True)


def counts_as_attended(mark: str) -> bool:
    return mark in ATTENDED_MARKS


@dataclass(frozen=True)
class MarkTally:
    roster: int
    present: int
    absent: int
    late: int
    excused: int
    unmarked: int


def tally(roster_size: int, marks: Iterable[str]) -> MarkTally:
    counts = {status: 0 for status in MARK_STATUSES}
    for mark in marks:
        counts[mark] += 1
    marked = sum(counts.values())
    if marked > roster_size:
        raise ValueError("more marks than students on the roster")
    return MarkTally(
        roster=roster_size, present=counts["present"], absent=counts["absent"], late=counts["late"],
        excused=counts["excused"], unmarked=roster_size - marked,
    )


def in_window(start: datetime, end: datetime, now: datetime) -> bool:
    """[start, end)."""
    return start <= now < end


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def pick_current(
    windows: Iterable[tuple[str, datetime, datetime, Optional[datetime]]], now: datetime
) -> Optional[int]:
    """Index of the class that is current at ``now``, or None.

    ``windows`` is ``(status, scheduled_start, scheduled_end, actual_started_at)``.
    An ACTIVE session is current whatever the clock says (a class that started
    early or is running over is still live); among several, the latest started.
    Otherwise the class whose scheduled window contains ``now``.
    """
    items = list(windows)
    active = [(i, w) for i, w in enumerate(items) if w[0] == SessionState.ACTIVE.value]
    if active:
        return max(active, key=lambda iw: iw[1][3] or iw[1][1])[0]
    for i, (_, start, end, _) in enumerate(items):
        if in_window(start, end, now):
            return i
    return None


# ---------------------------------------------------------------------------
# Phase 17: department monitoring -- is a class running, done, late or missed?
# ---------------------------------------------------------------------------

DEFAULT_START_GRACE_MINUTES = 10


class ClassState(str, Enum):
    UPCOMING = "upcoming"      # before its scheduled start
    DUE = "due"                # started time has passed, still inside the grace period
    DELAYED = "delayed"        # past start + grace, before its end, no session started
    NOT_HELD = "not_held"      # its scheduled end has passed and it was never started
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


NOT_STARTED_STATES = frozenset({ClassState.DELAYED, ClassState.NOT_HELD})


def class_state(status: str, scheduled_start: datetime, scheduled_end: datetime, now: datetime, grace_minutes: int) -> ClassState:
    """Deterministic state of one class meeting at ``now``.

    A scheduled class with no ACTIVE/CLOSED session is DELAYED once ``now`` is at
    or past ``scheduled_start + grace_minutes`` (and before its end), and
    NOT_HELD once its scheduled end has passed.
    """
    for value in (scheduled_start, scheduled_end, now):
        _aware(value)
    if grace_minutes < 0:
        raise ValueError("grace_minutes must not be negative")
    if status == SessionState.CANCELLED.value:
        return ClassState.CANCELLED
    if status == SessionState.ACTIVE.value:
        return ClassState.ACTIVE
    if status == SessionState.CLOSED.value:
        return ClassState.COMPLETED
    if now < scheduled_start:
        return ClassState.UPCOMING
    if now >= scheduled_end:
        return ClassState.NOT_HELD
    if now < scheduled_start + timedelta(minutes=grace_minutes):
        return ClassState.DUE
    return ClassState.DELAYED
