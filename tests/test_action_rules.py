"""Unit tests for deterministic pre-action precondition checks
(app/rules/action_preconditions.py) -- boundary conditions in particular
(exact deadline, exact capacity), not just the happy path, per CLAUDE.md's
Testing Rules.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.rules.action_preconditions import (
    CATEGORY_DEPARTMENTS,
    check_calendar_creation,
    check_case_creation,
    check_event_registration,
)
from app.schemas.events import EventSummary

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _event(**overrides) -> EventSummary:
    defaults = dict(
        event_id=1, title="AI Workshop", description="d", category="workshop", organizer="AI/ML Club",
        location="Auditorium 1", start_at=NOW + timedelta(days=5), end_at=NOW + timedelta(days=5, hours=2),
        registration_deadline=NOW + timedelta(days=4), capacity=100, status="open",
    )
    defaults.update(overrides)
    return EventSummary(**defaults)


def _checks_by_name(checks) -> dict:
    return {c.name: c for c in checks}


# ---------------------------------------------------------------------------
# check_event_registration
# ---------------------------------------------------------------------------


def test_student_not_found_short_circuits() -> None:
    checks = check_event_registration(
        student_exists=False, event=None, confirmed_registrations=0, already_registered=False, now=NOW
    )
    assert len(checks) == 1
    assert checks[0].name == "student_exists" and not checks[0].passed


def test_event_not_found_short_circuits_after_student_exists() -> None:
    checks = check_event_registration(
        student_exists=True, event=None, confirmed_registrations=0, already_registered=False, now=NOW
    )
    names = _checks_by_name(checks)
    assert names["student_exists"].passed
    assert not names["event_exists"].passed


def test_exact_capacity_is_full_not_available() -> None:
    checks = check_event_registration(
        student_exists=True, event=_event(capacity=10), confirmed_registrations=10, already_registered=False, now=NOW
    )
    assert not _checks_by_name(checks)["capacity_available"].passed


def test_one_under_capacity_is_available() -> None:
    checks = check_event_registration(
        student_exists=True, event=_event(capacity=10), confirmed_registrations=9, already_registered=False, now=NOW
    )
    assert _checks_by_name(checks)["capacity_available"].passed


def test_exact_deadline_instant_is_still_open() -> None:
    deadline = NOW
    checks = check_event_registration(
        student_exists=True, event=_event(registration_deadline=deadline), confirmed_registrations=0,
        already_registered=False, now=NOW,
    )
    assert _checks_by_name(checks)["registration_deadline"].passed


def test_one_second_past_deadline_is_closed() -> None:
    deadline = NOW - timedelta(seconds=1)
    checks = check_event_registration(
        student_exists=True, event=_event(registration_deadline=deadline), confirmed_registrations=0,
        already_registered=False, now=NOW,
    )
    assert not _checks_by_name(checks)["registration_deadline"].passed


def test_already_registered_fails() -> None:
    checks = check_event_registration(
        student_exists=True, event=_event(), confirmed_registrations=0, already_registered=True, now=NOW
    )
    assert not _checks_by_name(checks)["not_already_registered"].passed


def test_not_open_status_fails() -> None:
    checks = check_event_registration(
        student_exists=True, event=_event(status="closed"), confirmed_registrations=0, already_registered=False, now=NOW
    )
    assert not _checks_by_name(checks)["registration_open"].passed


def test_missing_conflict_data_is_flagged_not_silently_passed() -> None:
    checks = check_event_registration(
        student_exists=True, event=_event(), confirmed_registrations=0, already_registered=False, now=NOW,
        conflict_check_performed=False,
    )
    names = _checks_by_name(checks)
    assert not names["schedule_conflict_checked"].passed
    assert "no_schedule_conflicts" not in names


def test_conflicts_found_are_reported() -> None:
    from app.schemas.events import TimetableConflict

    checks = check_event_registration(
        student_exists=True, event=_event(), confirmed_registrations=0, already_registered=False, now=NOW,
        conflict_check_performed=True,
        timetable_conflicts=[TimetableConflict(course_code="CS301", weekday=0, start_time="10:00", end_time="11:00")],
    )
    names = _checks_by_name(checks)
    assert names["schedule_conflict_checked"].passed
    assert not names["no_schedule_conflicts"].passed


# ---------------------------------------------------------------------------
# check_calendar_creation
# ---------------------------------------------------------------------------


def test_calendar_student_not_found_short_circuits() -> None:
    checks = check_calendar_creation(
        student_exists=False, referenced_event_exists=None, duplicate_entry_exists=False, overlapping_entry_count=0
    )
    assert len(checks) == 1 and not checks[0].passed


def test_calendar_duplicate_entry_fails() -> None:
    checks = check_calendar_creation(
        student_exists=True, referenced_event_exists=None, duplicate_entry_exists=True, overlapping_entry_count=0
    )
    assert not _checks_by_name(checks)["no_duplicate_entry"].passed


def test_calendar_overlap_is_flagged() -> None:
    checks = check_calendar_creation(
        student_exists=True, referenced_event_exists=None, duplicate_entry_exists=False, overlapping_entry_count=1
    )
    assert not _checks_by_name(checks)["no_schedule_overlap"].passed


def test_calendar_referenced_event_missing_fails() -> None:
    checks = check_calendar_creation(
        student_exists=True, referenced_event_exists=False, duplicate_entry_exists=False, overlapping_entry_count=0
    )
    assert not _checks_by_name(checks)["referenced_event_exists"].passed


# ---------------------------------------------------------------------------
# check_case_creation
# ---------------------------------------------------------------------------


def test_case_valid_category_and_department_pass() -> None:
    for category, department in CATEGORY_DEPARTMENTS.items():
        checks = check_case_creation(
            student_exists=True, category=category, priority="normal", department=department, description_present=True
        )
        names = _checks_by_name(checks)
        assert names["valid_category"].passed
        assert names["supported_destination_department"].passed


def test_case_invalid_category_fails() -> None:
    checks = check_case_creation(
        student_exists=True, category="parking", priority="normal", department="Facilities Office", description_present=True
    )
    names = _checks_by_name(checks)
    assert not names["valid_category"].passed
    assert "supported_destination_department" not in names


def test_case_mismatched_department_fails() -> None:
    checks = check_case_creation(
        student_exists=True, category="hostel", priority="normal", department="Accounts Office", description_present=True
    )
    assert not _checks_by_name(checks)["supported_destination_department"].passed


def test_case_invalid_priority_fails() -> None:
    checks = check_case_creation(
        student_exists=True, category="hostel", priority="critical", department="Hostel Office", description_present=True
    )
    assert not _checks_by_name(checks)["valid_priority"].passed


def test_case_missing_description_fails() -> None:
    checks = check_case_creation(
        student_exists=True, category="hostel", priority="normal", department="Hostel Office", description_present=False
    )
    assert not _checks_by_name(checks)["required_information_present"].passed
