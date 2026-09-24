"""Deterministic class schedule and live-class status (Phase 16).

Shared by the faculty workspace and the student side. A day's classes for a
set of teaching assignments are the recurring timetable slots on that
weekday plus any extra-class sessions recorded for that date. Where an
``attendance_sessions`` row exists for a meeting, its status and timestamps
are the truth; where none exists yet the meeting is simply SCHEDULED.

Student-side reads never write. The faculty side materializes the day's
rows (idempotent, unique per assignment and start) so every class has an id
it can start.

Every answer here -- "has my class started?", "was I marked present?" --
comes from these rows, never from an LLM.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.academic import Enrollment, TimetableSlot
from app.db.models.faculty import (
    AttendanceSession,
    AttendanceSessionStatus,
    SessionAttendanceMark,
    TeachingAssignment,
)
from app.db.models.identity import Student
from app.db.repositories.students import get_student_by_id
from app.rules.class_session import pick_current
from app.schemas.faculty import LiveClassInfo, LiveClassStatus

CAMPUS_TZ = timezone(timedelta(hours=5, minutes=30))
CAMPUS_TZ_NAME = "Asia/Kolkata (IST)"
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def local(value: datetime) -> datetime:
    return value.astimezone(CAMPUS_TZ)


def hhmm(value: datetime) -> str:
    return local(value).strftime("%H:%M")


def clock(value: datetime) -> str:
    """'10:04 AM' in campus time."""
    return local(value).strftime("%I:%M %p").lstrip("0")


@dataclass
class PlannedClass:
    assignment: TeachingAssignment
    start: datetime
    end: datetime
    room: str
    slot_id: Optional[int]
    session: Optional[AttendanceSession]

    @property
    def status(self) -> str:
        return self.session.status.value if self.session else AttendanceSessionStatus.SCHEDULED.value

    @property
    def is_extra(self) -> bool:
        return self.slot_id is None


# ---------------------------------------------------------------------------
# Assignments and rosters
# ---------------------------------------------------------------------------


def student_assignments(session: Session, student: Student) -> List[TeachingAssignment]:
    """The teaching assignments that cover this student's enrollments and section."""
    if not student.section:
        return []
    enrollments = session.execute(select(Enrollment).where(Enrollment.student_id == student.id)).scalars().all()
    if not enrollments:
        return []
    found: List[TeachingAssignment] = []
    for enrollment in enrollments:
        assignment = session.execute(
            select(TeachingAssignment).where(
                TeachingAssignment.course_id == enrollment.course_id,
                TeachingAssignment.academic_term == enrollment.academic_year,
                TeachingAssignment.section == student.section,
            )
        ).scalar_one_or_none()
        if assignment is not None:
            found.append(assignment)
    return found


def roster(session: Session, assignment: TeachingAssignment) -> List[Student]:
    return list(
        session.execute(
            select(Student)
            .join(Enrollment, Enrollment.student_id == Student.id)
            .where(
                Enrollment.course_id == assignment.course_id,
                Enrollment.academic_year == assignment.academic_term,
                Student.section == assignment.section,
            )
            .order_by(Student.student_code)
        ).scalars().all()
    )


def on_roster(session: Session, assignment: TeachingAssignment, student: Student) -> bool:
    return student.section == assignment.section and session.execute(
        select(Enrollment.id).where(
            Enrollment.student_id == student.id,
            Enrollment.course_id == assignment.course_id,
            Enrollment.academic_year == assignment.academic_term,
        )
    ).first() is not None


# ---------------------------------------------------------------------------
# A day's classes
# ---------------------------------------------------------------------------


def planned_classes(session: Session, assignments: Sequence[TeachingAssignment], day: date) -> List[PlannedClass]:
    if not assignments:
        return []
    ids = [a.id for a in assignments]
    rows = session.execute(
        select(AttendanceSession).where(
            AttendanceSession.teaching_assignment_id.in_(ids), AttendanceSession.session_date == day
        )
    ).scalars().all()
    by_key: Dict[tuple, AttendanceSession] = {(r.teaching_assignment_id, r.scheduled_start): r for r in rows}
    used: set = set()
    planned: List[PlannedClass] = []
    for assignment in assignments:
        slots = session.execute(
            select(TimetableSlot).where(
                TimetableSlot.course_id == assignment.course_id, TimetableSlot.weekday == day.weekday()
            )
        ).scalars().all()
        for slot in slots:
            start = datetime.combine(day, slot.start_time, tzinfo=CAMPUS_TZ).astimezone(timezone.utc)
            end = datetime.combine(day, slot.end_time, tzinfo=CAMPUS_TZ).astimezone(timezone.utc)
            row = by_key.get((assignment.id, start))
            if row is not None:
                used.add(row.id)
            planned.append(PlannedClass(assignment, start, end, row.room if row else slot.location, slot.id, row))
    by_assignment = {a.id: a for a in assignments}
    for row in rows:
        if row.id not in used:
            planned.append(PlannedClass(
                by_assignment[row.teaching_assignment_id], row.scheduled_start, row.scheduled_end, row.room,
                row.timetable_slot_id, row,
            ))
    planned.sort(key=lambda p: (p.start, p.assignment.id))
    return planned


def materialize(session: Session, planned: Iterable[PlannedClass]) -> List[PlannedClass]:
    """Give every planned class a SCHEDULED row if it has none (idempotent)."""
    result = list(planned)
    for item in result:
        if item.session is not None:
            continue
        row = AttendanceSession(
            teaching_assignment_id=item.assignment.id, course_id=item.assignment.course_id,
            faculty_id=item.assignment.faculty_id, timetable_slot_id=item.slot_id,
            session_date=local(item.start).date(), scheduled_start=item.start, scheduled_end=item.end,
            room=item.room, status=AttendanceSessionStatus.SCHEDULED,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            # Another request materialized the same meeting first: use theirs.
            session.rollback()
            row = session.execute(
                select(AttendanceSession).where(
                    AttendanceSession.teaching_assignment_id == item.assignment.id,
                    AttendanceSession.scheduled_start == item.start,
                )
            ).scalar_one()
        item.session = row
    return result


# ---------------------------------------------------------------------------
# Live status
# ---------------------------------------------------------------------------


def _info(item: PlannedClass) -> LiveClassInfo:
    row = item.session
    return LiveClassInfo(
        session_id=row.id if row else None, course_code=item.assignment.course.code,
        course_title=item.assignment.course.title, section=item.assignment.section,
        faculty_name=item.assignment.faculty.full_name, room=item.room, scheduled_start=item.start,
        scheduled_end=item.end, start_local=hhmm(item.start), end_local=hhmm(item.end), session_status=item.status,
        is_extra_class=item.is_extra, actual_started_at=row.actual_started_at if row else None,
        actual_closed_at=row.actual_closed_at if row else None,
    )


def get_live_session(session: Session, assignment: TeachingAssignment, now: datetime) -> Optional[PlannedClass]:
    """The meeting of one taught class that is current at ``now``, if any."""
    planned = planned_classes(session, [assignment], local(now).date())
    index = pick_current(((p.status, p.start, p.end, p.session.actual_started_at if p.session else None) for p in planned), now)
    return planned[index] if index is not None else None


def get_student_attendance_for_session(session: Session, session_id: int, student_pk: int) -> Optional[SessionAttendanceMark]:
    return session.execute(
        select(SessionAttendanceMark).where(
            SessionAttendanceMark.session_id == session_id, SessionAttendanceMark.student_id == student_pk
        )
    ).scalar_one_or_none()


def _next(planned: Sequence[PlannedClass], now: datetime, exclude: Optional[PlannedClass]) -> Optional[PlannedClass]:
    return next(
        (p for p in planned if p is not exclude and p.start > now and p.status == AttendanceSessionStatus.SCHEDULED.value),
        None,
    )


def _next_sentence(upcoming: Optional[PlannedClass]) -> str:
    if upcoming is None:
        return " You have no more classes scheduled today."
    return (
        f" Your next class is {upcoming.assignment.course.title} at {clock(upcoming.start)} in {upcoming.room}."
    )


_MARK_WORDS = {"present": "present", "absent": "absent", "late": "late", "excused": "excused"}


def get_current_class(session: Session, student_id: str, now: datetime) -> LiveClassStatus:
    """The student's class at ``now`` and its real session/attendance state."""
    base = {"as_of": now, "timezone": CAMPUS_TZ_NAME}
    student = get_student_by_id(session, student_id)
    if student is None:
        return LiveClassStatus(state="no_class", message="Your student record was not found.", **base)
    planned = planned_classes(session, student_assignments(session, student), local(now).date())
    index = pick_current(((p.status, p.start, p.end, p.session.actual_started_at if p.session else None) for p in planned), now)
    current = planned[index] if index is not None else None
    upcoming = _next(planned, now, current)
    next_info = _info(upcoming) if upcoming else None

    if current is None:
        return LiveClassStatus(
            state="no_class", next_class=next_info,
            message="You do not have a class scheduled right now." + _next_sentence(upcoming), **base,
        )

    info = _info(current)
    title = current.assignment.course.title
    teacher = current.assignment.faculty.full_name
    window = f"{clock(current.start)}–{clock(current.end)}"
    row = current.session
    if current.status == AttendanceSessionStatus.SCHEDULED.value:
        return LiveClassStatus(
            state="scheduled", current=info, next_class=next_info,
            message=f"{title} is scheduled for {clock(current.start)}, but the faculty has not started the class session yet.",
            **base,
        )
    if current.status == AttendanceSessionStatus.CANCELLED.value:
        return LiveClassStatus(
            state="cancelled", current=info, next_class=next_info,
            message=f"{title} ({window}) has been cancelled today." + _next_sentence(upcoming), **base,
        )

    mark = get_student_attendance_for_session(session, row.id, student.id)
    my_attendance = mark.status.value if mark else "not_marked"
    if current.status == AttendanceSessionStatus.ACTIVE.value:
        message = f"Yes. {title} started at {clock(row.actual_started_at)}. Attendance was opened by {teacher}, and "
        message += (
            f"your attendance was marked {_MARK_WORDS[mark.status.value]} at {clock(mark.marked_at)}."
            if mark else "you have not been marked yet."
        )
        return LiveClassStatus(
            state="live", current=info, my_attendance=my_attendance, next_class=next_info,
            my_attendance_marked_at=mark.marked_at if mark else None, message=message, **base,
        )
    message = f"{title} ({window}) has ended; {teacher} closed the session at {clock(row.actual_closed_at)}."
    message += f" You were marked {_MARK_WORDS[mark.status.value]}." if mark else " You were not marked in it."
    return LiveClassStatus(
        state="closed", current=info, my_attendance=my_attendance, next_class=next_info,
        my_attendance_marked_at=mark.marked_at if mark else None, message=message + _next_sentence(upcoming), **base,
    )
