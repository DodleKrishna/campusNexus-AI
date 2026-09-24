"""Deterministic reviewer routing for student workflow requests (Phase 16).

The LLM never chooses a recipient. Given the classes a request affects (each
with the faculty assigned to teach it) and the student's mentor:

* EVENT_PERMISSION, OD_REQUEST, ATTENDANCE_PERMISSION: when the affected
  classes are all taught by one faculty member, that faculty member reviews
  it. Otherwise (no affected class, or several faculty) the mentor reviews it.
* LEAVE_REQUEST: the mentor reviews it; without a mentor, the one affected
  faculty member if there is exactly one.
* No reviewer found: status NEEDS_REVIEW, reviewer None. Never a guess.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

CLASS_FIRST = frozenset({"event_permission", "od_request", "attendance_permission"})
MENTOR_FIRST = frozenset({"leave_request"})
REQUEST_TYPES = CLASS_FIRST | MENTOR_FIRST


@dataclass(frozen=True)
class Reviewer:
    faculty_id: int
    name: str


@dataclass(frozen=True)
class RoutingDecision:
    reviewer: Optional[Reviewer]
    basis: str  # affected_course_faculty / mentor / unresolved
    note: str

    @property
    def resolved(self) -> bool:
        return self.reviewer is not None


def _single(affected: Iterable[Reviewer]) -> Optional[Reviewer]:
    distinct = {r.faculty_id: r for r in affected}
    return next(iter(distinct.values())) if len(distinct) == 1 else None


def route_request(request_type: str, affected_faculty: Iterable[Reviewer], mentor: Optional[Reviewer]) -> RoutingDecision:
    if request_type not in REQUEST_TYPES:
        raise ValueError(f"unknown request type {request_type!r}")
    affected = list(affected_faculty)
    single = _single(affected)
    distinct = len({r.faculty_id for r in affected})

    def by_class(r: Reviewer) -> RoutingDecision:
        return RoutingDecision(r, "affected_course_faculty", f"Sent to {r.name}, who teaches the affected class.")

    def by_mentor(r: Reviewer, why: str) -> RoutingDecision:
        return RoutingDecision(r, "mentor", f"Sent to your mentor {r.name} ({why}).")

    if request_type in CLASS_FIRST:
        if single is not None:
            return by_class(single)
        why = "no class is affected" if distinct == 0 else "classes taught by different faculty are affected"
        if mentor is not None:
            return by_mentor(mentor, why)
        return RoutingDecision(None, "unresolved", f"No reviewer could be determined: {why} and no mentor is assigned.")

    if mentor is not None:
        return by_mentor(mentor, "leave requests go to your mentor")
    if single is not None:
        return by_class(single)
    return RoutingDecision(None, "unresolved", "No reviewer could be determined: no mentor is assigned.")
