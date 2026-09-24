"""Department operations for a Head of Department (Phase 17). Read-only analytics.

Scope is resolved once, server-side, by ``hod_scope``: the signed-in account
must have the HOD role, be linked to a faculty profile, AND that profile must
be the department's recorded head (``departments.hod_faculty_id``). Every
function below takes that ``HodScope`` and only ever queries rows whose
department is ``scope.department`` -- a client never supplies a department.

Every status, count and percentage is computed here from timetable slots,
attendance sessions, marks, counters and requests. Class state (delayed / not
held / ...) comes from ``app/rules/class_session.class_state`` with a
configurable grace period; attendance standing from ``compute_attendance``.

The only writes are idempotent side effects of looking: escalating a student
request that no faculty reviewer can take (``escalate_unassigned``) and a
one-per-class delayed-class warning to the HOD. Both are audited/de-duplicated.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.academic import AttendanceRecord, Enrollment
from app.db.models.auth import AuthAccount
from app.db.models.faculty import AttendanceSession, AttendanceSessionStatus, FacultyProfile, SessionAttendanceMark, TeachingAssignment
from app.db.models.identity import Department, Student
from app.db.models.services import CampusCase, CaseStatus
from app.db.models.workflow import WorkflowRequest, WorkflowRequestStatus
from app.rules import class_session as rules
from app.rules.attendance import compute_attendance
from app.rules.attendance_standing import AttendanceStanding, attendance_standing
from app.rules.sla import compute_sla_breaches
from app.schemas.department import (
    AttendanceInsights,
    CourseAttendanceSummary,
    DepartmentClassView,
    DepartmentComplaint,
    DepartmentDashboard,
    DepartmentOverview,
    FacultySummary,
    HodProfileView,
    LowAttendanceEntry,
    SectionAttendanceSummary,
    SessionSummary,
    StudentRisk,
)
from app.schemas.enums import UserRole
from app.schemas.faculty import MarkTallyView
from app.services import staff_notifications
from app.services.class_schedule import PlannedClass, hhmm, local, planned_classes, roster
from app.services.knowledge import KnowledgeService

GRACE_ENV = "CAMPUSNEXUS_CLASS_START_GRACE_MINUTES"
FACULTY_TYPES = ("faculty_leave", "class_substitution", "department_permission")


def start_grace_minutes() -> int:
    """Grace period after a scheduled start before a class counts as delayed (default 10)."""
    raw = os.environ.get(GRACE_ENV)
    try:
        value = int(raw) if raw not in (None, "") else rules.DEFAULT_START_GRACE_MINUTES
    except ValueError:
        value = rules.DEFAULT_START_GRACE_MINUTES
    return max(0, value)


@dataclass(frozen=True)
class HodScope:
    account: AuthAccount
    faculty: FacultyProfile
    department: Department


def hod_scope(session: Session, account_id: int) -> Optional[HodScope]:
    """current HOD -> faculty identity -> department, or None. Role alone is never enough."""
    account = session.get(AuthAccount, account_id)
    if account is None or account.role != UserRole.HOD or account.linked_faculty_id is None:
        return None
    faculty = session.get(FacultyProfile, account.linked_faculty_id)
    if faculty is None:
        return None
    department = session.get(Department, faculty.department_id)
    if department is None or department.hod_faculty_id != faculty.id:
        return None
    return HodScope(account=account, faculty=faculty, department=department)


def department_head(session: Session, department_id: Optional[int]) -> Optional[FacultyProfile]:
    department = session.get(Department, department_id) if department_id else None
    if department is None or department.hod_faculty_id is None:
        return None
    return session.get(FacultyProfile, department.hod_faculty_id)


def class_label(assignment: TeachingAssignment) -> str:
    return f"{assignment.department.code} {assignment.year}-{assignment.section}"


# ---------------------------------------------------------------------------
# Department membership
# ---------------------------------------------------------------------------


def assignments(session: Session, scope: HodScope) -> List[TeachingAssignment]:
    return list(session.execute(
        select(TeachingAssignment).where(TeachingAssignment.department_id == scope.department.id).order_by(TeachingAssignment.id)
    ).scalars().all())


def faculty_members(session: Session, scope: HodScope) -> List[FacultyProfile]:
    return list(session.execute(
        select(FacultyProfile).where(FacultyProfile.department_id == scope.department.id).order_by(FacultyProfile.employee_code)
    ).scalars().all())


def students(session: Session, scope: HodScope) -> List[Student]:
    return list(session.execute(
        select(Student).where(Student.department_id == scope.department.id).order_by(Student.year, Student.section, Student.student_code)
    ).scalars().all())


# ---------------------------------------------------------------------------
# Today's classes
# ---------------------------------------------------------------------------


def _tally(session: Session, item: PlannedClass) -> Optional[MarkTallyView]:
    if item.session is None or item.status not in ("active", "closed"):
        return None
    marks = session.execute(
        select(SessionAttendanceMark.status).where(SessionAttendanceMark.session_id == item.session.id)
    ).scalars().all()
    t = rules.tally(len(roster(session, item.assignment)), (m.value for m in marks))
    return MarkTallyView(roster=t.roster, present=t.present, absent=t.absent, late=t.late, excused=t.excused, unmarked=t.unmarked)


def class_view(session: Session, item: PlannedClass, now: datetime, grace: int) -> DepartmentClassView:
    a, row = item.assignment, item.session
    return DepartmentClassView(
        session_id=row.id if row else None, assignment_id=a.id, course_code=a.course.code, course_title=a.course.title,
        class_label=class_label(a), year=a.year, section=a.section, faculty_id=a.faculty_id, faculty_name=a.faculty.full_name,
        room=item.room, scheduled_start=item.start, scheduled_end=item.end, start_local=hhmm(item.start), end_local=hhmm(item.end),
        session_status=item.status, state=rules.class_state(item.status, item.start, item.end, now, grace).value,
        is_extra_class=item.is_extra, actual_started_at=row.actual_started_at if row else None,
        actual_closed_at=row.actual_closed_at if row else None, tally=_tally(session, item),
    )


def activity(session: Session, scope: HodScope, now: datetime) -> List[DepartmentClassView]:
    grace = start_grace_minutes()
    return [class_view(session, p, now, grace) for p in planned_classes(session, assignments(session, scope), local(now).date())]


def not_started(views: Sequence[DepartmentClassView]) -> List[DepartmentClassView]:
    return [v for v in views if v.state in (rules.ClassState.DELAYED.value, rules.ClassState.NOT_HELD.value)]


def warn_delayed(session: Session, scope: HodScope, views: Sequence[DepartmentClassView], now: datetime) -> None:
    """One delayed-class warning per class meeting to the HOD (idempotent)."""
    for v in views:
        if v.state == rules.ClassState.DELAYED.value:
            staff_notifications.notify(
                session, scope.faculty.id, f"Class not started: {v.course_title}",
                f"{v.course_title} ({v.class_label}, {v.start_local}–{v.end_local}, {v.room}) with {v.faculty_name} "
                f"has not been started {start_grace_minutes()} minutes after its scheduled time.",
                "delayed_class", now=now, ref_key=f"delayed:{v.assignment_id}:{v.scheduled_start.isoformat()}",
            )
    session.commit()


# ---------------------------------------------------------------------------
# Requests (escalation + counts)
# ---------------------------------------------------------------------------


def escalate_unassigned(session: Session, scope: HodScope, now: datetime) -> int:
    """Route submitted student requests of this department that no faculty could take to the HOD."""
    from app.services import workflow_requests

    rows = session.execute(
        select(WorkflowRequest).where(
            WorkflowRequest.department_id == scope.department.id, WorkflowRequest.status == WorkflowRequestStatus.NEEDS_REVIEW,
            WorkflowRequest.reviewer_faculty_id.is_(None), WorkflowRequest.student_id.is_not(None),
        )
    ).scalars().all()
    for request in rows:
        workflow_requests.escalate_to_hod(session, request, scope.faculty, scope.department, now)
    if rows:
        session.commit()
    return len(rows)


def _requests_to(session: Session, scope: HodScope) -> List[WorkflowRequest]:
    return list(session.execute(
        select(WorkflowRequest).where(
            WorkflowRequest.reviewer_faculty_id == scope.faculty.id, WorkflowRequest.status == WorkflowRequestStatus.PENDING,
        )
    ).scalars().all())


def pending_faculty_requests(session: Session, scope: HodScope) -> List[WorkflowRequest]:
    return [r for r in _requests_to(session, scope) if r.requester_faculty_id is not None]


def escalated_student_requests(session: Session, scope: HodScope) -> List[WorkflowRequest]:
    return [r for r in _requests_to(session, scope) if r.student_id is not None and r.routing_basis == "hod_escalation"]


# ---------------------------------------------------------------------------
# Dashboard, faculty list
# ---------------------------------------------------------------------------


def profile(scope: HodScope) -> HodProfileView:
    f, d = scope.faculty, scope.department
    return HodProfileView(
        faculty_id=f.id, employee_code=f.employee_code, full_name=f.full_name, designation=f.designation, email=f.email,
        department_id=d.id, department_code=d.code, department_name=d.name,
    )


def dashboard(session: Session, scope: HodScope, now: datetime) -> DepartmentDashboard:
    escalate_unassigned(session, scope, now)
    views = activity(session, scope, now)
    warn_delayed(session, scope, views, now)
    counted = [v for v in views if v.state != rules.ClassState.CANCELLED.value]
    return DepartmentDashboard(
        profile=profile(scope), date=local(now).date(), now_local=local(now).strftime("%H:%M"), start_grace_minutes=start_grace_minutes(),
        faculty_count=len(faculty_members(session, scope)), student_count=len(students(session, scope)),
        classes_today=len(counted), active_classes=sum(1 for v in views if v.state == "active"),
        completed_classes=sum(1 for v in views if v.state == "completed"), not_started_classes=len(not_started(views)),
        pending_faculty_requests=len(pending_faculty_requests(session, scope)),
        escalated_student_requests=len(escalated_student_requests(session, scope)), activity=views,
    )


def faculty_list(session: Session, scope: HodScope, now: datetime) -> List[FacultySummary]:
    views = activity(session, scope, now)
    by_faculty: Dict[int, List[DepartmentClassView]] = {}
    for v in views:
        by_faculty.setdefault(v.faculty_id, []).append(v)
    teaching: Dict[int, List[str]] = {}
    for a in assignments(session, scope):
        teaching.setdefault(a.faculty_id, []).append(f"{a.course.code} ({class_label(a)})")
    result = []
    for f in faculty_members(session, scope):
        mine = [v for v in by_faculty.get(f.id, []) if v.state != "cancelled"]
        active = next((v for v in mine if v.state == "active"), None)
        to_review = session.execute(select(WorkflowRequest.id).where(
            WorkflowRequest.reviewer_faculty_id == f.id, WorkflowRequest.status == WorkflowRequestStatus.PENDING)).all()
        own_open = session.execute(select(WorkflowRequest.id).where(
            WorkflowRequest.requester_faculty_id == f.id,
            WorkflowRequest.status.in_([WorkflowRequestStatus.PENDING, WorkflowRequestStatus.NEEDS_REVIEW]))).all()
        result.append(FacultySummary(
            faculty_id=f.id, employee_code=f.employee_code, full_name=f.full_name, designation=f.designation, email=f.email,
            is_hod=scope.department.hod_faculty_id == f.id, courses=teaching.get(f.id, []), classes_today=len(mine),
            active_class=f"{active.course_title} ({active.class_label})" if active else None,
            not_started_today=len(not_started(mine)), pending_requests_to_review=len(to_review), own_open_requests=len(own_open),
        ))
    return result


# ---------------------------------------------------------------------------
# Attendance analytics
# ---------------------------------------------------------------------------


def _required(knowledge: KnowledgeService, now: datetime) -> Optional[float]:
    from app.services.faculty_ops import required_percentage

    return required_percentage(knowledge, now)


def _counters(session: Session, a: TeachingAssignment) -> List[tuple]:
    """(student, AttendanceRecord) for every rostered student of one assignment."""
    rows = session.execute(
        select(Student, AttendanceRecord)
        .join(Enrollment, Enrollment.student_id == Student.id)
        .join(AttendanceRecord, AttendanceRecord.enrollment_id == Enrollment.id)
        .where(Enrollment.course_id == a.course_id, Enrollment.academic_year == a.academic_term, Student.section == a.section)
        .order_by(Student.student_code)
    ).all()
    return [(s, r) for s, r in rows]


def _below(record: AttendanceRecord, required: Optional[float]) -> Optional[bool]:
    if required is None:
        return None
    calc = compute_attendance(record.classes_attended, record.classes_conducted, Decimal(str(required)))
    standing = attendance_standing(calc)
    if standing == AttendanceStanding.UNKNOWN:
        return None
    return standing == AttendanceStanding.BELOW_REQUIREMENT


def _pct(attended: int, conducted: int) -> Optional[float]:
    if conducted == 0:
        return None
    return float((Decimal(attended) * 100 / Decimal(conducted)).quantize(Decimal("0.1")))


def _session_summary(session: Session, row: AttendanceSession) -> SessionSummary:
    a = row.teaching_assignment
    marks = session.execute(select(SessionAttendanceMark.status).where(SessionAttendanceMark.session_id == row.id)).scalars().all()
    t = rules.tally(len(roster(session, a)), (m.value for m in marks))
    return SessionSummary(
        session_id=row.id, course_code=a.course.code, course_title=a.course.title, class_label=class_label(a),
        faculty_name=a.faculty.full_name, session_date=row.session_date, start_local=hhmm(row.scheduled_start),
        status=row.status.value, actual_started_at=row.actual_started_at,
        tally=MarkTallyView(roster=t.roster, present=t.present, absent=t.absent, late=t.late, excused=t.excused, unmarked=t.unmarked),
    )


def attendance_insights(session: Session, knowledge: KnowledgeService, scope: HodScope, now: datetime) -> AttendanceInsights:
    required = _required(knowledge, now)
    courses: List[CourseAttendanceSummary] = []
    sections: Dict[tuple, dict] = {}
    low: List[LowAttendanceEntry] = []
    for a in assignments(session, scope):
        pairs = _counters(session, a)
        attended = sum(r.classes_attended for _, r in pairs)
        conducted = sum(r.classes_conducted for _, r in pairs)
        below = [(s, r) for s, r in pairs if _below(r, required)]
        courses.append(CourseAttendanceSummary(
            assignment_id=a.id, course_code=a.course.code, course_title=a.course.title, class_label=class_label(a),
            faculty_name=a.faculty.full_name, students=len(pairs), classes_attended=attended, classes_conducted=conducted,
            percentage=_pct(attended, conducted), below_threshold=len(below),
        ))
        key = (a.year, a.section)
        bucket = sections.setdefault(key, {"label": class_label(a), "students": set(), "attended": 0, "conducted": 0, "below": set()})
        bucket["students"].update(s.id for s, _ in pairs)
        bucket["attended"] += attended
        bucket["conducted"] += conducted
        bucket["below"].update(s.id for s, _ in below)
        low += [
            LowAttendanceEntry(
                student_id=s.student_code, full_name=s.user.full_name, class_label=class_label(a), course_code=a.course.code,
                course_title=a.course.title, classes_attended=r.classes_attended, classes_conducted=r.classes_conducted,
                percentage=_pct(r.classes_attended, r.classes_conducted) or 0.0,
            )
            for s, r in below
        ]
    ids = [a.id for a in assignments(session, scope)]
    recent_rows = session.execute(
        select(AttendanceSession).where(
            AttendanceSession.teaching_assignment_id.in_(ids),
            AttendanceSession.status.in_([AttendanceSessionStatus.ACTIVE, AttendanceSessionStatus.CLOSED]),
        ).order_by(AttendanceSession.actual_started_at.desc()).limit(10)
    ).scalars().all() if ids else []
    recent = [_session_summary(session, r) for r in recent_rows]
    active_rows = session.execute(
        select(AttendanceSession).where(AttendanceSession.teaching_assignment_id.in_(ids), AttendanceSession.status == AttendanceSessionStatus.ACTIVE)
    ).scalars().all() if ids else []
    incomplete = [s for s in (_session_summary(session, r) for r in active_rows) if s.tally.unmarked > 0]
    views = activity(session, scope, now)
    return AttendanceInsights(
        required_percentage=required,
        sections=[
            SectionAttendanceSummary(
                class_label=b["label"], year=year, section=section, students=len(b["students"]), classes_attended=b["attended"],
                classes_conducted=b["conducted"], percentage=_pct(b["attended"], b["conducted"]), below_threshold=len(b["below"]),
            )
            for (year, section), b in sorted(sections.items())
        ],
        courses=courses, low_attendance=sorted(low, key=lambda e: (e.percentage, e.student_id)),
        recent_sessions=recent, incomplete_sessions=incomplete,
        not_held_today=[v for v in views if v.state == rules.ClassState.NOT_HELD.value],
    )


def student_risk(session: Session, knowledge: KnowledgeService, scope: HodScope, now: datetime) -> List[StudentRisk]:
    required = _required(knowledge, now)
    result: List[StudentRisk] = []
    for s in students(session, scope):
        rows = session.execute(
            select(Enrollment, AttendanceRecord).join(AttendanceRecord, AttendanceRecord.enrollment_id == Enrollment.id)
            .where(Enrollment.student_id == s.id)
        ).all()
        attended = sum(r.classes_attended for _, r in rows)
        conducted = sum(r.classes_conducted for _, r in rows)
        open_cases = session.execute(select(CampusCase.id).where(
            CampusCase.student_id == s.id, CampusCase.status.in_([CaseStatus.OPEN, CaseStatus.IN_PROGRESS]))).all()
        result.append(StudentRisk(
            student_id=s.student_code, full_name=s.user.full_name, year=s.year, semester=s.semester, section=s.section,
            overall_percentage=_pct(attended, conducted), classes_attended=attended, classes_conducted=conducted,
            courses_below_threshold=[e.course.code for e, r in rows if _below(r, required)], open_complaints=len(open_cases),
        ))
    result.sort(key=lambda r: (-len(r.courses_below_threshold), r.overall_percentage if r.overall_percentage is not None else 101, r.student_id))
    return result


def present_today(session: Session, scope: HodScope, now: datetime, year: Optional[int], section: Optional[str]) -> List[SessionSummary]:
    """Started/closed sessions today for the department's classes, optionally one year/section."""
    chosen = [a for a in assignments(session, scope)
              if (year is None or a.year == year) and (section is None or a.section.upper() == section.upper())]
    ids = [a.id for a in chosen]
    if not ids:
        return []
    rows = session.execute(select(AttendanceSession).where(
        AttendanceSession.teaching_assignment_id.in_(ids), AttendanceSession.session_date == local(now).date(),
        AttendanceSession.status.in_([AttendanceSessionStatus.ACTIVE, AttendanceSessionStatus.CLOSED]),
    ).order_by(AttendanceSession.scheduled_start)).scalars().all()
    return [_session_summary(session, r) for r in rows]


# ---------------------------------------------------------------------------
# Complaints of the department's students
# ---------------------------------------------------------------------------


def complaints(session: Session, scope: HodScope, now: datetime) -> List[DepartmentComplaint]:
    rows = session.execute(
        select(CampusCase).join(Student, Student.id == CampusCase.student_id)
        .where(Student.department_id == scope.department.id).order_by(CampusCase.created_at.desc())
    ).scalars().all()
    result = []
    for case in rows:
        response_breached = resolution_breached = False
        if case.sla is not None:
            response_breached, resolution_breached = compute_sla_breaches(
                response_due_at=case.sla.response_due_at, resolution_due_at=case.sla.resolution_due_at,
                responded_at=case.sla.responded_at, resolved_at=case.sla.resolved_at, now=now,
            )
        result.append(DepartmentComplaint(
            case_code=case.case_code, student_id=case.student.student_code, student_name=case.student.user.full_name,
            category=case.category, priority=case.priority.value, status=case.status.value, office=case.department,
            created_at=case.created_at, response_due_at=case.sla.response_due_at if case.sla else None,
            resolution_due_at=case.sla.resolution_due_at if case.sla else None,
            response_breached=response_breached, resolution_breached=resolution_breached,
        ))
    return result


def overview(session: Session, knowledge: KnowledgeService, scope: HodScope, now: datetime) -> DepartmentOverview:
    risk = student_risk(session, knowledge, scope, now)
    return DepartmentOverview(
        dashboard=dashboard(session, scope, now), faculty=faculty_list(session, scope, now),
        at_risk_students=sum(1 for r in risk if r.courses_below_threshold),
        below_threshold_entries=sum(len(r.courses_below_threshold) for r in risk), required_percentage=_required(knowledge, now),
    )


def upcoming_window(now: datetime, days: int = 14) -> tuple[datetime, datetime]:
    return now, now + timedelta(days=days)
