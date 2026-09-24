"""Student portal composition (Phase 15): the signed-in student's own data.

A deterministic read service. It only composes existing services and rules:
the policy threshold is extracted from retrieved policy evidence
(``extract_attendance_threshold``) and every attendance decision comes from
``compute_attendance`` -- the same pipeline the Academic Agent uses. Nothing
here is calculated by, or passed through, an LLM.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.schemas.student_portal import (
    CourseAttendance,
    DashboardSummary,
    ExamItem,
    NotificationItem,
    OverallAttendance,
    PolicySource,
    RequestItem,
    ScheduleSlot,
    StudentProfile,
    TodaySchedule,
)
from app.db.models.communication import Notification, NotificationStatus
from app.db.models.mission import ApprovalRecord, Mission, ToolCallRecord
from app.db.models.workflow import WorkflowRequest, WorkflowRequestStatus
from app.db.repositories.students import get_student_by_id
from app.rules.attendance import compute_attendance
from app.rules.attendance_standing import attendance_standing, slot_status
from app.rules.eligibility import compute_exam_eligibility
from app.rules.policy_threshold import extract_attendance_threshold
from app.schemas.academic import AttendanceCalculation, PolicyThreshold, ThresholdExtractionStatus
from app.schemas.enums import ApprovalStatus
from app.services import academic as academic_service
from app.services.knowledge import KnowledgeService

CAMPUS_TZ = timezone(timedelta(hours=5, minutes=30))
CAMPUS_TZ_NAME = "Asia/Kolkata (IST)"
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def profile(session: Session, student_id: str) -> Optional[StudentProfile]:
    student = get_student_by_id(session, student_id)
    if student is None:
        return None
    full_name = student.user.full_name
    return StudentProfile(
        student_code=student.student_code, full_name=full_name, first_name=full_name.split()[0],
        department_code=student.department.code, department_name=student.department.name,
        year=student.year, semester=student.semester, cgpa=student.cgpa,
    )


def _threshold(knowledge: KnowledgeService, today: date) -> Tuple[PolicyThreshold, Optional[PolicySource]]:
    evidence = knowledge.get_active_policy("attendance_policy", as_of=today, visibility="public")
    threshold = extract_attendance_threshold(evidence)
    if threshold.status != ThresholdExtractionStatus.OK or threshold.document_id is None:
        return threshold, None
    title = next((e.title for e in evidence if e.document_id == threshold.document_id), None)
    return threshold, PolicySource(
        document_id=threshold.document_id, title=title, version=threshold.policy_version, section=threshold.section
    )


def attendance_threshold(knowledge: KnowledgeService, today: date) -> Tuple[PolicyThreshold, Optional[PolicySource]]:
    """The attendance policy threshold from retrieved policy evidence (Phase 16: shared with the faculty side)."""
    return _threshold(knowledge, today)


def _instructors(session: Session, student_id: str) -> Dict[str, str]:
    academic = academic_service.get_student_academic_profile(session, student_id)
    return {c.course_code: c.instructor for c in academic.courses} if academic else {}


def attendance(session: Session, knowledge: KnowledgeService, student_id: str) -> List[CourseAttendance]:
    threshold, source = _threshold(knowledge, _now().date())
    instructors = _instructors(session, student_id)
    items: List[CourseAttendance] = []
    for snap in academic_service.get_attendance(session, student_id):
        calc: Optional[AttendanceCalculation] = None
        if threshold.required_percentage is not None:
            calc = compute_attendance(snap.classes_attended, snap.classes_conducted, threshold.required_percentage)
        items.append(CourseAttendance(
            course_code=snap.course_code, course_title=snap.course_title, instructor=instructors.get(snap.course_code),
            classes_attended=snap.classes_attended, classes_conducted=snap.classes_conducted,
            current_percentage=float(calc.current_percentage) if calc and calc.current_percentage is not None else None,
            required_percentage=float(threshold.required_percentage) if threshold.required_percentage is not None else None,
            eligible_now=calc.eligible_now if calc else None,
            standing=attendance_standing(calc).value,
            classes_needed_to_reach_threshold=calc.classes_needed_to_reach_threshold if calc else None,
            maximum_additional_absences_allowed=calc.maximum_additional_absences_allowed if calc else None,
            threshold_reachable=calc.threshold_reachable if calc else None,
            policy=source,
        ))
    return items


def overall_attendance(items: List[CourseAttendance]) -> Optional[OverallAttendance]:
    attended = sum(i.classes_attended for i in items)
    conducted = sum(i.classes_conducted for i in items)
    if conducted == 0:
        return None
    percentage = (Decimal(attended) * 100 / Decimal(conducted)).quantize(Decimal("0.1"))
    return OverallAttendance(percentage=float(percentage), classes_attended=attended, classes_conducted=conducted)


def exams(session: Session, knowledge: KnowledgeService, student_id: str, *, upcoming_only: bool = True) -> List[ExamItem]:
    now = _now()
    threshold, _ = _threshold(knowledge, now.date())
    counters = {s.course_code: s for s in academic_service.get_attendance(session, student_id)}
    items: List[ExamItem] = []
    for exam in academic_service.get_exam_schedule(session, student_id):
        starts, ends = datetime.fromisoformat(exam.scheduled_start), datetime.fromisoformat(exam.scheduled_end)
        if upcoming_only and ends < now:
            continue
        eligibility = caveat = None
        snap = counters.get(exam.course_code)
        if snap is not None and threshold.required_percentage is not None:
            result = compute_exam_eligibility(
                compute_attendance(snap.classes_attended, snap.classes_conducted, threshold.required_percentage)
            )
            eligibility, caveat = result.status.value, result.caveat
        items.append(ExamItem(
            course_code=exam.course_code, course_title=exam.course_title, exam_type=exam.exam_type,
            starts_at=starts, ends_at=ends, venue=exam.location, eligibility=eligibility, eligibility_caveat=caveat,
        ))
    return sorted(items, key=lambda e: e.starts_at)


def today_schedule(session: Session, student_id: str, *, now: Optional[datetime] = None) -> TodaySchedule:
    local = (now or _now()).astimezone(CAMPUS_TZ)
    instructors = _instructors(session, student_id)
    slots = [
        ScheduleSlot(
            course_code=s.course_code, course_title=s.course_title, instructor=instructors.get(s.course_code),
            start_time=s.start_time, end_time=s.end_time, location=s.location,
            status=slot_status(s.start_time, s.end_time, local.time()).value,
        )
        for s in academic_service.get_timetable(session, student_id)
        if s.weekday == local.weekday()
    ]
    slots.sort(key=lambda s: s.start_time)
    return TodaySchedule(
        date=local.date(), weekday=_WEEKDAYS[local.weekday()], timezone=CAMPUS_TZ_NAME,
        now_local=local.strftime("%H:%M"), slots=slots,
    )


def requests(session: Session, student_id: str) -> List[RequestItem]:
    """Actions requested through this student's missions, newest first."""
    rows = session.execute(
        select(ApprovalRecord, ToolCallRecord.tool_name)
        .join(Mission, Mission.mission_id == ApprovalRecord.mission_id)
        .outerjoin(ToolCallRecord, ToolCallRecord.tool_call_id == ApprovalRecord.tool_call_id)
        .where(Mission.user_id == student_id)
        .order_by(ApprovalRecord.created_at.desc())
    ).all()
    return [
        RequestItem(
            approval_id=a.approval_id, mission_id=a.mission_id, title=a.action_summary, status=a.status.value,
            tool_name=tool, requested_at=a.created_at, decided_at=a.decision_at,
        )
        for a, tool in rows
    ]


def notifications(session: Session, student_id: str, limit: int = 20) -> List[NotificationItem]:
    student = get_student_by_id(session, student_id)
    if student is None:
        return []
    rows = session.execute(
        select(Notification).where(Notification.student_id == student.id)
        .order_by(Notification.created_at.desc()).limit(limit)
    ).scalars().all()
    return [
        NotificationItem(id=n.id, title=n.title, body=n.body, category=n.category, status=n.status.value, created_at=n.created_at)
        for n in rows
    ]


def _open_workflow_requests(session: Session, student_id: str) -> int:
    """Phase 16: permission/leave/OD requests sent and still waiting for a decision."""
    return session.execute(
        select(func.count()).select_from(WorkflowRequest).where(
            WorkflowRequest.student_id == student_id,
            WorkflowRequest.status.in_([WorkflowRequestStatus.PENDING, WorkflowRequestStatus.NEEDS_REVIEW]),
        )
    ).scalar_one()


def dashboard(session: Session, knowledge: KnowledgeService, student_id: str) -> Optional[DashboardSummary]:
    student = profile(session, student_id)
    if student is None:
        return None
    courses = attendance(session, knowledge, student_id)
    upcoming = exams(session, knowledge, student_id)
    notes = notifications(session, student_id, limit=100)
    return DashboardSummary(
        profile=student, cgpa=student.cgpa, overall_attendance=overall_attendance(courses),
        courses_below_requirement=sum(1 for c in courses if c.standing == "below_requirement"),
        next_exam=upcoming[0] if upcoming else None,
        pending_requests=sum(1 for r in requests(session, student_id) if r.status == ApprovalStatus.PENDING.value)
        + _open_workflow_requests(session, student_id),
        unread_notifications=sum(1 for n in notes if n.status != NotificationStatus.READ.value),
    )
