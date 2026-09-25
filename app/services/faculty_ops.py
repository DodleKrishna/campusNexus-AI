"""Faculty class operations (Phase 16): deterministic, authenticated, audited.

Every function takes the caller's ``FacultyProfile`` (resolved by the API
from the JWT's account link, never from the client) and only ever touches
that faculty member's own teaching assignments and sessions. A session that
belongs to someone else raises ``FacultyAccessError``; a lifecycle rule that
refuses (``app/rules/class_session.py``) raises ``OperationRefused``.

Attendance is written only here, only through these explicit operations.
No LLM output ever reaches these functions.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.tenancy import active_membership
from app.db.models.academic import AttendanceRecord, Enrollment, TimetableSlot
from app.db.models.auth import AuthAccount
from app.db.models.communication import Notification, NotificationStatus
from app.db.models.faculty import (
    AttendanceMarkStatus,
    AttendanceSession,
    AttendanceSessionStatus,
    FacultyProfile,
    SessionAttendanceMark,
    TeachingAssignment,
)
from app.db.models.identity import Student
from app.db.models.workflow import WorkflowRequest, WorkflowRequestStatus
from app.db.repositories import operations_audit
from app.rules import class_session as rules
from app.rules.attendance import compute_attendance
from app.rules.attendance_standing import AttendanceStanding, attendance_standing
from app.schemas.faculty import (
    AssignmentStanding,
    FacultyClassDetail,
    FacultyClassView,
    FacultyDashboard,
    FacultyProfileView,
    MarkTallyView,
    RosterEntry,
    TeachingAssignmentView,
    WeeklySlotView,
)
from app.services.class_schedule import (
    WEEKDAYS,
    PlannedClass,
    hhmm,
    local,
    materialize,
    planned_classes,
    roster,
)
from app.services.knowledge import KnowledgeService


class FacultyAccessError(Exception):
    """The class or record is not one of this faculty member's."""


class OperationRefused(Exception):
    """A deterministic lifecycle rule refused the operation (the message says why)."""


def faculty_for_account(session: Session, account_id: int) -> Optional[FacultyProfile]:
    """Phase 22B: the faculty profile of the account's active organization membership (tenant-scoped)."""
    membership = active_membership(session, account_id)
    if membership is None or membership.faculty_profile_id is None:
        return None
    return session.get(FacultyProfile, membership.faculty_profile_id)


def assignments_of(session: Session, faculty: FacultyProfile) -> List[TeachingAssignment]:
    return list(
        session.execute(
            select(TeachingAssignment).where(TeachingAssignment.faculty_id == faculty.id).order_by(TeachingAssignment.id)
        ).scalars().all()
    )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def assignment_view(session: Session, assignment: TeachingAssignment) -> TeachingAssignmentView:
    slots = session.execute(
        select(TimetableSlot).where(TimetableSlot.course_id == assignment.course_id).order_by(TimetableSlot.weekday, TimetableSlot.start_time)
    ).scalars().all()
    return TeachingAssignmentView(
        assignment_id=assignment.id, course_code=assignment.course.code, course_title=assignment.course.title,
        department_code=assignment.department.code, year=assignment.year, semester=assignment.semester,
        section=assignment.section, academic_term=assignment.academic_term, roster_size=len(roster(session, assignment)),
        weekly_slots=[
            WeeklySlotView(weekday=WEEKDAYS[s.weekday], start_time=s.start_time.strftime("%H:%M"), end_time=s.end_time.strftime("%H:%M"), room=s.location)
            for s in slots
        ],
    )


def profile_view(session: Session, faculty: FacultyProfile) -> FacultyProfileView:
    return FacultyProfileView(
        faculty_id=faculty.id, employee_code=faculty.employee_code, full_name=faculty.full_name,
        department_code=faculty.department.code, department_name=faculty.department.name,
        designation=faculty.designation, email=faculty.email, phone=faculty.phone,
        assignments=[assignment_view(session, a) for a in assignments_of(session, faculty)],
    )


def _marks(session: Session, session_id: int) -> List[SessionAttendanceMark]:
    return list(session.execute(select(SessionAttendanceMark).where(SessionAttendanceMark.session_id == session_id)).scalars().all())


def _tally(session: Session, row: AttendanceSession, roster_size: int) -> MarkTallyView:
    t = rules.tally(roster_size, (m.status.value for m in _marks(session, row.id)))
    return MarkTallyView(roster=t.roster, present=t.present, absent=t.absent, late=t.late, excused=t.excused, unmarked=t.unmarked)


def class_view(session: Session, row: AttendanceSession, now: datetime, *, roster_size: Optional[int] = None) -> FacultyClassView:
    assignment = row.teaching_assignment
    size = roster_size if roster_size is not None else len(roster(session, assignment))
    decision = rules.can_start(row.status.value, row.scheduled_start, row.scheduled_end, now)
    return FacultyClassView(
        session_id=row.id, assignment_id=assignment.id, course_code=assignment.course.code,
        course_title=assignment.course.title, department_code=assignment.department.code, year=assignment.year,
        semester=assignment.semester, section=assignment.section, session_date=row.session_date,
        scheduled_start=row.scheduled_start, scheduled_end=row.scheduled_end, start_local=hhmm(row.scheduled_start),
        end_local=hhmm(row.scheduled_end), room=row.room, status=row.status.value,
        is_extra_class=row.timetable_slot_id is None, actual_started_at=row.actual_started_at,
        actual_closed_at=row.actual_closed_at, tally=_tally(session, row, size), can_start=decision.allowed,
        start_blocked_reason=None if decision.allowed or row.status != AttendanceSessionStatus.SCHEDULED else decision.reason,
    )


def todays_classes(session: Session, faculty: FacultyProfile, now: datetime) -> List[PlannedClass]:
    return materialize(session, planned_classes(session, assignments_of(session, faculty), local(now).date()))


def classes_today(session: Session, faculty: FacultyProfile, now: datetime) -> List[FacultyClassView]:
    return [class_view(session, p.session, now) for p in todays_classes(session, faculty, now)]


def owned_session(session: Session, faculty: FacultyProfile, session_id: int) -> AttendanceSession:
    row = session.get(AttendanceSession, session_id)
    if row is None or row.teaching_assignment.faculty_id != faculty.id:
        raise FacultyAccessError("This class is not assigned to you.")
    return row


def owned_assignment(session: Session, faculty: FacultyProfile, assignment_id: int) -> TeachingAssignment:
    assignment = session.get(TeachingAssignment, assignment_id)
    if assignment is None or assignment.faculty_id != faculty.id:
        raise FacultyAccessError("This class is not assigned to you.")
    return assignment


def course_counters(session: Session, assignment: TeachingAssignment, student: Student) -> Optional[AttendanceRecord]:
    return session.execute(
        select(AttendanceRecord).join(Enrollment, Enrollment.id == AttendanceRecord.enrollment_id).where(
            Enrollment.student_id == student.id, Enrollment.course_id == assignment.course_id,
            Enrollment.academic_year == assignment.academic_term,
        )
    ).scalar_one_or_none()


def _standing_entry(
    session: Session, assignment: TeachingAssignment, student: Student, required: Optional[float],
    mark: Optional[SessionAttendanceMark] = None,
) -> RosterEntry:
    counters = course_counters(session, assignment, student)
    calc = None
    if counters is not None and required is not None:
        calc = compute_attendance(counters.classes_attended, counters.classes_conducted, Decimal(str(required)))
    return RosterEntry(
        student_id=student.student_code, full_name=student.user.full_name,
        mark=mark.status.value if mark else None, marked_at=mark.marked_at if mark else None,
        classes_attended=counters.classes_attended if counters else None,
        classes_conducted=counters.classes_conducted if counters else None,
        current_percentage=float(calc.current_percentage) if calc and calc.current_percentage is not None else None,
        standing=attendance_standing(calc).value,
    )


def required_percentage(knowledge: KnowledgeService, now: datetime) -> Optional[float]:
    from app.services.student_portal import attendance_threshold

    threshold, _ = attendance_threshold(knowledge, now.date())
    return float(threshold.required_percentage) if threshold.required_percentage is not None else None


def class_detail(session: Session, knowledge: KnowledgeService, faculty: FacultyProfile, session_id: int, now: datetime) -> FacultyClassDetail:
    row = owned_session(session, faculty, session_id)
    students = roster(session, row.teaching_assignment)
    marks = {m.student_id: m for m in _marks(session, row.id)}
    required = required_percentage(knowledge, now)
    return FacultyClassDetail(
        class_info=class_view(session, row, now, roster_size=len(students)),
        roster=[_standing_entry(session, row.teaching_assignment, s, required, marks.get(s.id)) for s in students],
        required_percentage=required,
    )


def attendance_overview(session: Session, knowledge: KnowledgeService, faculty: FacultyProfile, now: datetime) -> List[AssignmentStanding]:
    required = required_percentage(knowledge, now)
    result: List[AssignmentStanding] = []
    for assignment in assignments_of(session, faculty):
        entries = [_standing_entry(session, assignment, s, required) for s in roster(session, assignment)]
        result.append(AssignmentStanding(
            assignment=assignment_view(session, assignment), required_percentage=required,
            below_requirement=sum(1 for e in entries if e.standing == AttendanceStanding.BELOW_REQUIREMENT.value),
            at_risk=sum(1 for e in entries if e.standing == AttendanceStanding.AT_RISK.value),
            students=entries,
        ))
    return result


def pending_request_count(session: Session, faculty: FacultyProfile) -> int:
    return session.execute(
        select(func.count()).select_from(WorkflowRequest).where(
            WorkflowRequest.reviewer_faculty_id == faculty.id, WorkflowRequest.status == WorkflowRequestStatus.PENDING
        )
    ).scalar_one()


def dashboard(session: Session, faculty: FacultyProfile, now: datetime) -> FacultyDashboard:
    planned = todays_classes(session, faculty, now)
    views = [class_view(session, p.session, now) for p in planned]
    running = [v for v in views if v.status == AttendanceSessionStatus.ACTIVE.value]
    students: set = set()
    for p in planned:
        if p.status != AttendanceSessionStatus.CANCELLED.value:
            students.update(s.id for s in roster(session, p.assignment))
    # A class left open from an earlier day is still this faculty member's active class.
    if not running:
        stale = session.execute(
            select(AttendanceSession).where(
                AttendanceSession.faculty_id == faculty.id, AttendanceSession.status == AttendanceSessionStatus.ACTIVE
            )
        ).scalars().first()
        if stale is not None:
            running = [class_view(session, stale, now)]
    return FacultyDashboard(
        profile=profile_view(session, faculty), date=local(now).date(), now_local=local(now).strftime("%H:%M"),
        classes_today=sum(1 for v in views if v.status != AttendanceSessionStatus.CANCELLED.value),
        active_class=running[0] if running else None, students_across_today=len(students),
        pending_requests=pending_request_count(session, faculty), today=views,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _refuse_if(decision: rules.RuleDecision) -> None:
    if not decision.allowed:
        raise OperationRefused(decision.reason)


def _audit(session: Session, account: AuthAccount, event_type: str, row: AttendanceSession, message: str, **metadata) -> None:
    operations_audit.record(
        session, event_type=event_type, actor_account_id=account.id, actor_role=account.role.value,
        subject_type="attendance_session", subject_id=str(row.id), message=message, metadata=metadata,
    )


def start_class(session: Session, faculty: FacultyProfile, account: AuthAccount, session_id: int, now: datetime) -> AttendanceSession:
    row = owned_session(session, faculty, session_id)
    _refuse_if(rules.can_start(row.status.value, row.scheduled_start, row.scheduled_end, now))
    other = session.execute(
        select(AttendanceSession).where(
            AttendanceSession.faculty_id == faculty.id, AttendanceSession.status == AttendanceSessionStatus.ACTIVE,
            AttendanceSession.id != row.id,
        )
    ).scalars().first()
    if other is not None:
        raise OperationRefused(f"Close your class in session ({other.course.title}, {hhmm(other.scheduled_start)}) first.")
    row.status = AttendanceSessionStatus.ACTIVE
    row.actual_started_at = now
    _audit(session, account, "class_started", row, f"{faculty.full_name} started {row.course.title}.", started_at=now.isoformat())
    session.commit()
    return row


def mark_attendance(
    session: Session, faculty: FacultyProfile, account: AuthAccount, session_id: int, marks: Dict[str, str], now: datetime,
) -> AttendanceSession:
    """Set marks (student_code -> status). Every student must be on this class's roster."""
    row = owned_session(session, faculty, session_id)
    _refuse_if(rules.can_mark(row.status.value))
    students = {s.student_code: s for s in roster(session, row.teaching_assignment)}
    unknown = sorted(code for code in marks if code not in students)
    if unknown:
        raise OperationRefused(f"Not on this class's roster: {', '.join(unknown)}.")
    existing = {m.student_id: m for m in _marks(session, row.id)}
    changed: List[dict] = []
    for code, status in marks.items():
        value = AttendanceMarkStatus(status)
        student = students[code]
        mark = existing.get(student.id)
        if mark is None:
            session.add(SessionAttendanceMark(session_id=row.id, student_id=student.id, status=value, marked_at=now, marked_by_faculty_id=faculty.id))
        elif mark.status != value:
            mark.status, mark.marked_at, mark.marked_by_faculty_id = value, now, faculty.id
        else:
            continue
        changed.append({"student_id": code, "status": value.value})
    if changed:
        _audit(session, account, "attendance_marked", row, f"{faculty.full_name} marked {len(changed)} student(s).", marks=changed)
    session.commit()
    return row


def mark_all(session: Session, faculty: FacultyProfile, account: AuthAccount, session_id: int, status: str, now: datetime) -> AttendanceSession:
    row = owned_session(session, faculty, session_id)
    return mark_attendance(session, faculty, account, session_id, {s.student_code: status for s in roster(session, row.teaching_assignment)}, now)


def close_class(session: Session, faculty: FacultyProfile, account: AuthAccount, session_id: int, now: datetime) -> AttendanceSession:
    """Close the session and add it to every rostered student's counters, exactly once."""
    row = owned_session(session, faculty, session_id)
    assignment = row.teaching_assignment
    students = roster(session, assignment)
    marks = {m.student_id: m for m in _marks(session, row.id)}
    _refuse_if(rules.can_close(row.status.value, sum(1 for s in students if s.id not in marks)))
    row.status = AttendanceSessionStatus.CLOSED
    row.actual_closed_at = now
    day = local(row.scheduled_start).strftime("%a %d %b")
    for student in students:
        mark = marks[student.id].status.value
        counters = course_counters(session, assignment, student)
        if counters is None:
            enrollment = session.execute(
                select(Enrollment).where(
                    Enrollment.student_id == student.id, Enrollment.course_id == assignment.course_id,
                    Enrollment.academic_year == assignment.academic_term,
                )
            ).scalar_one()
            counters = AttendanceRecord(enrollment_id=enrollment.id, classes_attended=0, classes_conducted=0)
            session.add(counters)
        counters.classes_conducted += 1
        if rules.counts_as_attended(mark):
            counters.classes_attended += 1
        counters.last_updated_at = now
        session.add(Notification(
            student_id=student.id, title=f"Attendance recorded: {assignment.course.title}",
            body=f"You were marked {mark} in {assignment.course.title} on {day} ({hhmm(row.scheduled_start)}–{hhmm(row.scheduled_end)}).",
            category="attendance", status=NotificationStatus.SENT, sent_at=now,
        ))
    tally = rules.tally(len(students), (m.status.value for m in marks.values()))
    _audit(
        session, account, "class_closed", row, f"{faculty.full_name} closed {assignment.course.title}.",
        closed_at=now.isoformat(), present=tally.present, absent=tally.absent, late=tally.late, excused=tally.excused,
    )
    session.commit()
    return row


def cancel_class(session: Session, faculty: FacultyProfile, account: AuthAccount, session_id: int, now: datetime, note: Optional[str] = None) -> AttendanceSession:
    row = owned_session(session, faculty, session_id)
    _refuse_if(rules.can_cancel(row.status.value))
    row.status = AttendanceSessionStatus.CANCELLED
    row.note = note
    for student in roster(session, row.teaching_assignment):
        session.add(Notification(
            student_id=student.id, title=f"Class cancelled: {row.course.title}",
            body=f"{row.course.title} on {local(row.scheduled_start).strftime('%a %d %b')} at {hhmm(row.scheduled_start)} has been cancelled by {faculty.full_name}.",
            category="academic", status=NotificationStatus.SENT, sent_at=now,
        ))
    _audit(session, account, "class_cancelled", row, f"{faculty.full_name} cancelled {row.course.title}.", note=note)
    session.commit()
    return row


def sessions_on(session: Session, faculty: FacultyProfile, assignment_ids: Sequence[int], now: datetime) -> List[PlannedClass]:
    """Today's classes for some of this faculty member's assignments (read-only)."""
    mine = [a for a in assignments_of(session, faculty) if a.id in set(assignment_ids)]
    return planned_classes(session, mine, local(now).date())
