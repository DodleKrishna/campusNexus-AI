"""Deterministic pre-action precondition checks for the three write tools.

Pure functions over already-fetched DB rows / DTOs -- no I/O here, callers
(``app/verification/action.py``) supply already-queried data, mirroring the
shape of ``app/rules/event_availability.py`` and ``app/rules/sla.py``. Each
returns a list of named ``VerificationCheck`` results rather than a single
bool, so a failure is always traceable to a specific named rule.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any, List, Mapping, Optional

from app.rules.event_availability import compute_availability
from app.schemas.action import TargetProvenance, TargetSource
from app.schemas.events import EventAvailabilityStatus, EventSummary, ExamConflict, TimetableConflict
from app.schemas.verification import VerificationCheck

# category -> the one department that category is routed to. Mirrors
# scripts/seed_data.py's CASE_SEED mapping exactly (hostel -> Hostel Office,
# it_helpdesk -> IT Helpdesk, fees -> Accounts Office, facilities ->
# Facilities Office, administrative -> Administrative Office) -- CLAUDE.md
# is explicit that a precondition rule must never invent a requirement that
# isn't already established, so this reuses the real seeded routing rather
# than a freshly-invented one.
CATEGORY_DEPARTMENTS = {
    "hostel": "Hostel Office",
    "it_helpdesk": "IT Helpdesk",
    "fees": "Accounts Office",
    "facilities": "Facilities Office",
    "administrative": "Administrative Office",
}
VALID_CASE_PRIORITIES = {"low", "normal", "high", "urgent"}

# The constraint naming each tool's target resource. A tool absent here has
# no external target to select (a complaint is routed by category); a
# calendar entry only has one when it is anchored to a campus event.
ACTION_TARGET_FIELDS = {"register_event": "event_title", "create_calendar_event": "source_event_title"}

_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")


def _normalize_for_match(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _NON_ALNUM_RE.sub(" ", folded).strip()


def target_named_in_goal(target: str, goal: str) -> bool:
    """True when ``target`` appears in ``goal`` as a whole-word phrase,
    ignoring case, quotes and punctuation. The student has to have said it."""
    normalized_target = _normalize_for_match(target)
    return bool(normalized_target) and f" {normalized_target} " in f" {_normalize_for_match(goal)} "


def resolve_target_provenance(
    tool_name: str, constraints: Mapping[str, Any], goal: Optional[str]
) -> TargetProvenance:
    """Decide whether an action's target was explicitly selected by the student.

    ``goal`` is the student's own request. ``None`` means no goal was
    supplied because the caller passed the constraints directly (an explicit
    human edit of an action) -- the caller is then the source. A planner's
    choice, an agent's recommendation or the top search result is never
    enough: the target must appear in what the student wrote.
    """
    field = ACTION_TARGET_FIELDS.get(tool_name)
    if field is None:
        return TargetProvenance(tool_name=tool_name, source=TargetSource.NOT_REQUIRED)

    raw_value = constraints.get(field)
    value = str(raw_value).strip() if raw_value is not None else ""
    has_event_id = constraints.get("event_id") is not None
    if tool_name == "create_calendar_event" and not value:
        return TargetProvenance(tool_name=tool_name, target_field=field, source=TargetSource.NOT_REQUIRED)

    if goal is None:
        if value or has_event_id:
            return TargetProvenance(
                tool_name=tool_name, target_field=field, target_value=value or None, source=TargetSource.CALLER_SUPPLIED
            )
        return TargetProvenance(
            tool_name=tool_name, target_field=field, source=TargetSource.UNCONFIRMED, reason="no target was specified."
        )

    if not value:
        return TargetProvenance(
            tool_name=tool_name,
            target_field=field,
            source=TargetSource.UNCONFIRMED,
            reason="no specific event was named in the request.",
        )
    if not target_named_in_goal(value, goal):
        return TargetProvenance(
            tool_name=tool_name,
            target_field=field,
            target_value=value,
            source=TargetSource.UNCONFIRMED,
            reason=f"'{value}' was not named in the request.",
        )
    return TargetProvenance(tool_name=tool_name, target_field=field, target_value=value, source=TargetSource.USER_GOAL)


def check_event_registration(
    *,
    student_exists: bool,
    event: Optional[EventSummary],
    confirmed_registrations: int,
    already_registered: bool,
    now: datetime,
    timetable_conflicts: Optional[List[TimetableConflict]] = None,
    exam_conflicts: Optional[List[ExamConflict]] = None,
    conflict_check_performed: bool = False,
) -> List[VerificationCheck]:
    checks: List[VerificationCheck] = [
        VerificationCheck(
            name="student_exists", passed=student_exists, detail=None if student_exists else "No student record found."
        )
    ]
    if not student_exists:
        return checks

    checks.append(
        VerificationCheck(
            name="event_exists", passed=event is not None, detail=None if event is not None else "No event found for the requested event_id."
        )
    )
    if event is None:
        return checks

    checks.append(
        VerificationCheck(
            name="not_already_registered",
            passed=not already_registered,
            detail=None if not already_registered else "Student already has a registration for this event.",
        )
    )

    # compute_availability short-circuits in priority order (open -> deadline
    # -> capacity), so when an earlier check already failed, the later ones
    # are reported passed (uninformative, not incorrect -- the precheck as a
    # whole already fails on the first blocking reason).
    availability = compute_availability(event, now=now, confirmed_registrations=confirmed_registrations)
    checks.append(
        VerificationCheck(
            name="registration_open",
            passed=availability != EventAvailabilityStatus.NOT_OPEN,
            detail=None if availability != EventAvailabilityStatus.NOT_OPEN else f"Event status is {event.status!r}, not open for registration.",
        )
    )
    checks.append(
        VerificationCheck(
            name="registration_deadline",
            passed=availability != EventAvailabilityStatus.REGISTRATION_CLOSED,
            detail=None if availability != EventAvailabilityStatus.REGISTRATION_CLOSED else "The registration deadline has passed.",
        )
    )
    checks.append(
        VerificationCheck(
            name="capacity_available",
            passed=availability != EventAvailabilityStatus.FULL,
            detail=None if availability != EventAvailabilityStatus.FULL else "Event is at capacity.",
        )
    )

    checks.append(
        VerificationCheck(
            name="schedule_conflict_checked",
            passed=conflict_check_performed,
            detail=None if conflict_check_performed else "No timetable/exam data was available to check for schedule conflicts.",
        )
    )
    if conflict_check_performed:
        conflicts = list(timetable_conflicts or []) + list(exam_conflicts or [])
        checks.append(
            VerificationCheck(
                name="no_schedule_conflicts",
                passed=not conflicts,
                detail=None if not conflicts else f"{len(conflicts)} schedule conflict(s) found.",
            )
        )
    return checks


def check_calendar_creation(
    *,
    student_exists: bool,
    referenced_event_exists: Optional[bool],
    duplicate_entry_exists: bool,
    overlapping_entry_count: int,
) -> List[VerificationCheck]:
    checks: List[VerificationCheck] = [
        VerificationCheck(
            name="student_exists", passed=student_exists, detail=None if student_exists else "No student record found."
        )
    ]
    if not student_exists:
        return checks

    if referenced_event_exists is not None:
        checks.append(
            VerificationCheck(
                name="referenced_event_exists",
                passed=referenced_event_exists,
                detail=None if referenced_event_exists else "The referenced source event/exam could not be found.",
            )
        )

    checks.append(
        VerificationCheck(
            name="no_duplicate_entry",
            passed=not duplicate_entry_exists,
            detail=None if not duplicate_entry_exists else "An identical calendar entry (same title and start time) already exists.",
        )
    )
    checks.append(
        VerificationCheck(
            name="no_schedule_overlap",
            passed=overlapping_entry_count == 0,
            detail=None if overlapping_entry_count == 0 else f"Overlaps {overlapping_entry_count} existing calendar entry/entries.",
        )
    )
    return checks


def check_case_creation(
    *,
    student_exists: bool,
    category: str,
    priority: str,
    department: str,
    description_present: bool,
) -> List[VerificationCheck]:
    checks: List[VerificationCheck] = [
        VerificationCheck(
            name="student_exists", passed=student_exists, detail=None if student_exists else "No student record found."
        )
    ]
    if not student_exists:
        return checks

    checks.append(
        VerificationCheck(
            name="required_information_present",
            passed=description_present,
            detail=None if description_present else "Case description is required.",
        )
    )

    valid_category = category in CATEGORY_DEPARTMENTS
    checks.append(
        VerificationCheck(
            name="valid_category",
            passed=valid_category,
            detail=None if valid_category else f"Unsupported case category {category!r}; supported: {sorted(CATEGORY_DEPARTMENTS)}.",
        )
    )

    valid_priority = priority in VALID_CASE_PRIORITIES
    checks.append(
        VerificationCheck(
            name="valid_priority",
            passed=valid_priority,
            detail=None if valid_priority else f"Unsupported case priority {priority!r}; supported: {sorted(VALID_CASE_PRIORITIES)}.",
        )
    )

    if valid_category:
        expected_department = CATEGORY_DEPARTMENTS[category]
        department_supported = department == expected_department
        checks.append(
            VerificationCheck(
                name="supported_destination_department",
                passed=department_supported,
                detail=None if department_supported else f"Category {category!r} routes to {expected_department!r}, not {department!r}.",
            )
        )
    return checks
