"""Deterministic event availability + schedule-conflict checking.

"Available" (registerable) requires: status OPEN, within the registration
window (if one is set), and under capacity (if one is set) -- an event must
never be recommended as available if it is full, closed, or outside its
registration period (CLAUDE.md/Phase 6 spec). A conflicted event is still
*returned*, just flagged, never hidden.

Timetable conflicts compare the event's absolute UTC instant against the
existing campus timezone strategy (IST = UTC+5:30, the same convention
``scripts/seed_data.py``/``docs/ARCHITECTURE.md`` already establish) since
``TimetableSlot`` is a recurring wall-clock IST slot with no calendar date.
Exam conflicts compare two absolute UTC instant ranges directly -- no
conversion needed, since both are stored as ``UTCDateTime``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from app.schemas.academic import ExamEntry, TimetableEntry
from app.schemas.events import (
    EventAssessment,
    EventAvailabilityStatus,
    EventSummary,
    ExamConflict,
    TimetableConflict,
)

IST = timezone(timedelta(hours=5, minutes=30))


def compute_availability(event: EventSummary, *, now: datetime, confirmed_registrations: int) -> EventAvailabilityStatus:
    if event.status != "open":
        return EventAvailabilityStatus.NOT_OPEN
    if event.registration_deadline is not None and now > event.registration_deadline:
        return EventAvailabilityStatus.REGISTRATION_CLOSED
    if event.capacity is not None and confirmed_registrations >= event.capacity:
        return EventAvailabilityStatus.FULL
    return EventAvailabilityStatus.AVAILABLE


def _overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


def find_timetable_conflicts(event: EventSummary, timetable: List[TimetableEntry]) -> List[TimetableConflict]:
    """Conflicts between an event and the student's recurring weekly timetable.

    Only handles events that start and end on the same IST calendar day
    (true for every seeded event, and for any realistic single-session
    workshop/talk) -- a cross-midnight event is out of scope for this phase
    and is simply not flagged, rather than raising.
    """
    start_ist = event.start_at.astimezone(IST)
    end_ist = event.end_at.astimezone(IST)
    if start_ist.date() != end_ist.date():
        return []

    weekday = start_ist.weekday()
    event_start_time = start_ist.time()
    event_end_time = end_ist.time()

    conflicts = []
    for slot in timetable:
        if slot.weekday != weekday:
            continue
        slot_start = datetime.strptime(slot.start_time, "%H:%M").time()
        slot_end = datetime.strptime(slot.end_time, "%H:%M").time()
        if event_start_time < slot_end and slot_start < event_end_time:
            conflicts.append(
                TimetableConflict(
                    course_code=slot.course_code, weekday=slot.weekday, start_time=slot.start_time, end_time=slot.end_time
                )
            )
    return conflicts


def find_exam_conflicts(event: EventSummary, exams: List[ExamEntry]) -> List[ExamConflict]:
    conflicts = []
    for exam in exams:
        exam_start = datetime.fromisoformat(exam.scheduled_start)
        exam_end = datetime.fromisoformat(exam.scheduled_end)
        if _overlaps(event.start_at, event.end_at, exam_start, exam_end):
            conflicts.append(
                ExamConflict(
                    course_code=exam.course_code,
                    exam_type=exam.exam_type,
                    scheduled_start=exam_start,
                    scheduled_end=exam_end,
                )
            )
    return conflicts


def assess_event(
    event: EventSummary,
    *,
    now: datetime,
    confirmed_registrations: int,
    already_registered: bool = False,
    registration_status: Optional[str] = None,
    timetable: Optional[List[TimetableEntry]] = None,
    exams: Optional[List[ExamEntry]] = None,
) -> EventAssessment:
    availability = compute_availability(event, now=now, confirmed_registrations=confirmed_registrations)
    conflict_check_performed = timetable is not None or exams is not None
    timetable_conflicts = find_timetable_conflicts(event, timetable) if timetable is not None else []
    exam_conflicts = find_exam_conflicts(event, exams) if exams is not None else []

    return EventAssessment(
        event=event,
        availability=availability,
        already_registered=already_registered,
        registration_status=registration_status,
        timetable_conflicts=timetable_conflicts,
        exam_conflicts=exam_conflicts,
        conflict_check_performed=conflict_check_performed,
    )
