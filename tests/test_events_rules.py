"""Unit tests for deterministic event availability/conflict checking (app/rules/event_availability.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.rules.event_availability import (
    assess_event,
    compute_availability,
    find_exam_conflicts,
    find_timetable_conflicts,
)
from app.schemas.academic import ExamEntry, TimetableEntry
from app.schemas.events import EventAvailabilityStatus, EventSummary

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _event(**overrides) -> EventSummary:
    defaults = dict(
        event_id=1,
        title="AI Workshop",
        description="A workshop on artificial intelligence.",
        category="workshop",
        organizer="AI/ML Club",
        location="Auditorium 1",
        start_at=NOW + timedelta(days=5),
        end_at=NOW + timedelta(days=5, hours=2),
        registration_deadline=NOW + timedelta(days=4),
        capacity=100,
        status="open",
    )
    defaults.update(overrides)
    return EventSummary(**defaults)


def test_available_when_open_within_deadline_under_capacity() -> None:
    assert compute_availability(_event(), now=NOW, confirmed_registrations=50) == EventAvailabilityStatus.AVAILABLE


def test_full_when_confirmed_registrations_meet_capacity() -> None:
    assert compute_availability(_event(capacity=10), now=NOW, confirmed_registrations=10) == EventAvailabilityStatus.FULL


def test_registration_closed_past_deadline() -> None:
    ev = _event(registration_deadline=NOW - timedelta(hours=1))
    assert compute_availability(ev, now=NOW, confirmed_registrations=0) == EventAvailabilityStatus.REGISTRATION_CLOSED


def test_not_open_when_status_is_not_open() -> None:
    assert compute_availability(_event(status="closed"), now=NOW, confirmed_registrations=0) == EventAvailabilityStatus.NOT_OPEN


def test_no_capacity_limit_is_always_available_on_that_dimension() -> None:
    ev = _event(capacity=None)
    assert compute_availability(ev, now=NOW, confirmed_registrations=10_000) == EventAvailabilityStatus.AVAILABLE


def test_no_registration_deadline_never_closes_on_that_dimension() -> None:
    ev = _event(registration_deadline=None)
    assert compute_availability(ev, now=NOW + timedelta(days=100), confirmed_registrations=0) == EventAvailabilityStatus.AVAILABLE


# ---------------------------------------------------------------------------
# Timetable conflicts (IST wall-clock vs. absolute UTC event instant)
# ---------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))


def test_timetable_conflict_detected_when_event_overlaps_class_slot() -> None:
    # Monday 10:30-12:00 IST event vs. Monday 10:00-11:00 IST class -> overlap.
    monday_ist = datetime(2026, 10, 5, 10, 30, tzinfo=IST)  # a Monday
    event = _event(start_at=monday_ist.astimezone(timezone.utc), end_at=(monday_ist + timedelta(hours=1, minutes=30)).astimezone(timezone.utc))
    timetable = [TimetableEntry(course_code="CS301", course_title="Operating Systems", weekday=0, start_time="10:00", end_time="11:00", location="Room 1")]
    conflicts = find_timetable_conflicts(event, timetable)
    assert len(conflicts) == 1
    assert conflicts[0].course_code == "CS301"


def test_no_timetable_conflict_on_a_different_weekday() -> None:
    tuesday_ist = datetime(2026, 10, 6, 10, 30, tzinfo=IST)  # a Tuesday
    event = _event(start_at=tuesday_ist.astimezone(timezone.utc), end_at=(tuesday_ist + timedelta(hours=1)).astimezone(timezone.utc))
    timetable = [TimetableEntry(course_code="CS301", course_title="Operating Systems", weekday=0, start_time="10:00", end_time="11:00", location="Room 1")]
    assert find_timetable_conflicts(event, timetable) == []


def test_no_timetable_conflict_when_times_do_not_overlap() -> None:
    monday_ist = datetime(2026, 10, 5, 12, 0, tzinfo=IST)  # same day, later time
    event = _event(start_at=monday_ist.astimezone(timezone.utc), end_at=(monday_ist + timedelta(hours=1)).astimezone(timezone.utc))
    timetable = [TimetableEntry(course_code="CS301", course_title="Operating Systems", weekday=0, start_time="10:00", end_time="11:00", location="Room 1")]
    assert find_timetable_conflicts(event, timetable) == []


# ---------------------------------------------------------------------------
# Exam conflicts (both absolute UTC instants, no timezone conversion needed)
# ---------------------------------------------------------------------------


def test_exam_conflict_detected_on_overlap() -> None:
    exam_start = NOW + timedelta(days=10)
    exam_end = exam_start + timedelta(hours=2)
    event = _event(start_at=exam_start + timedelta(hours=1), end_at=exam_start + timedelta(hours=3))
    exams = [
        ExamEntry(
            course_code="CS302", course_title="DBMS", exam_type="midterm",
            scheduled_start=exam_start.isoformat(), scheduled_end=exam_end.isoformat(), location="Hall 1",
        )
    ]
    conflicts = find_exam_conflicts(event, exams)
    assert len(conflicts) == 1
    assert conflicts[0].course_code == "CS302"


def test_no_exam_conflict_when_event_is_well_after_exam() -> None:
    exam_start = NOW + timedelta(days=10)
    exam_end = exam_start + timedelta(hours=2)
    event = _event(start_at=exam_end + timedelta(hours=5), end_at=exam_end + timedelta(hours=6))
    exams = [
        ExamEntry(
            course_code="CS302", course_title="DBMS", exam_type="midterm",
            scheduled_start=exam_start.isoformat(), scheduled_end=exam_end.isoformat(), location="Hall 1",
        )
    ]
    assert find_exam_conflicts(event, exams) == []


# ---------------------------------------------------------------------------
# assess_event: full assembly
# ---------------------------------------------------------------------------


def test_assess_event_without_conflict_data_reports_no_conflicts_but_marks_check_not_performed() -> None:
    assessment = assess_event(_event(), now=NOW, confirmed_registrations=0)
    assert assessment.timetable_conflicts == []
    assert assessment.exam_conflicts == []
    assert assessment.conflict_check_performed is False


def test_assess_event_with_conflict_data_marks_check_performed() -> None:
    assessment = assess_event(_event(), now=NOW, confirmed_registrations=0, timetable=[], exams=[])
    assert assessment.conflict_check_performed is True


def test_assess_event_reflects_registration_and_availability() -> None:
    assessment = assess_event(
        _event(capacity=1), now=NOW, confirmed_registrations=1, already_registered=True, registration_status="confirmed"
    )
    assert assessment.availability == EventAvailabilityStatus.FULL
    assert assessment.already_registered is True
    assert assessment.registration_status == "confirmed"
