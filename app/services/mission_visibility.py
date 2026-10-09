"""CAMPUS AI: read-only visibility of the autonomous AgentOS missions (Guardians and the Communication Agent).

Reuses ``agent_missions`` and each Guardian's own subject rows; there is no second mission table. Every mission is
reached only through a subject the caller may see, inside the caller's tenant session:

- admin: every Guardian / Communication mission in the organization;
- HOD: missions they own, missions of classes they teach and of classes in the department they head;
- faculty: missions they own and missions of classes they teach (``teaching_assignments.faculty_id``);
- student: only missions that concern them (an assignment/exam target, their own absence intervention, a
  communication job addressed to them), and only their own progress -- never class counts or other students.

The view is structured status and progress only. It never contains the mission goal or context text, step inputs or
outputs, a model's message, a contact detail, a transcript or audio. Staff get a bounded list of step identities
(action kind, tool name, status, time) -- the same facts the activity trail shows.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, List, Literal, Optional, Tuple

from pydantic import BaseModel
from sqlalchemy import Select, false, func, or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    AgentMission, AgentMissionStatus, AgentStep, Assignment, AssignmentStatus, AssignmentSubmission, AssignmentTarget,
    AttendanceIntervention, Course, Exam, Student, TeachingAssignment,
)
from app.db.models.communication_delivery import CommunicationJob
from app.db.models.exam import ExamAttendance, ExamAttendanceStatus, ExamTarget
from app.schemas.enums import UserRole
from app.services.assignments import headed_department_id

ASSIGNMENT_GUARDIAN = "assignment_guardian"
EXAM_GUARDIAN = "exam_guardian"
ATTENDANCE_GUARDIAN = "attendance_guardian"
COMMUNICATION_AGENT = "communication_agent"
AUTONOMOUS_AGENT_KEYS = (ASSIGNMENT_GUARDIAN, EXAM_GUARDIAN, ATTENDANCE_GUARDIAN, COMMUNICATION_AGENT)
AGENT_LABELS = {ASSIGNMENT_GUARDIAN: "Assignment Guardian", EXAM_GUARDIAN: "Exam Guardian",
                ATTENDANCE_GUARDIAN: "Attendance Guardian", COMMUNICATION_AGENT: "Communication Agent"}
MAX_LIMIT = 50
MAX_ACTIVITY = 8
_RESOLVED_EXAM = (ExamAttendanceStatus.PRESENT, ExamAttendanceStatus.EXEMPT, ExamAttendanceStatus.MAKEUP_COMPLETED)
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


class MissionSubject(BaseModel):
    type: Literal["assignment", "exam", "attendance", "communication"]
    title: str
    course_code: Optional[str] = None
    due_at: Optional[datetime] = None  # assignment deadline / exam start / absence detection time


class MissionProgress(BaseModel):
    """``scope='class'`` (staff): counts over the targets. ``scope='self'`` (student): only their own state."""

    scope: Literal["class", "self"]
    total: Optional[int] = None
    resolved: Optional[int] = None
    pending: Optional[int] = None
    own_status: Optional[str] = None  # an enumerated value (submitted / late / pending / present / open / delivered ...)


class MissionActivity(BaseModel):
    step_number: int
    action_type: str
    tool_name: Optional[str]
    status: str
    created_at: datetime


class AutonomousMissionView(BaseModel):
    mission_id: int
    agent_key: str
    agent_label: str
    kind: Literal["guardian", "communication"]
    status: AgentMissionStatus
    waiting_for: Optional[str]
    next_wake_at: Optional[datetime]
    step_count: int
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime]
    # The latest autonomous wake-up: ``checkpoint`` (its timer came due) or the waking domain event's type code.
    last_wake_at: Optional[datetime] = None
    last_wake_trigger: Optional[str] = None
    result_code: Optional[str]  # the supervisor's terminal machine code (staff only)
    subject: MissionSubject
    progress: Optional[MissionProgress]
    activity: List[MissionActivity]


class AutonomousMissionPage(BaseModel):
    items: List[AutonomousMissionView]
    next_before_id: Optional[int]


# --- Scope ----------------------------------------------------------------------------------------------------------


def _own_student_id(session: Session, actor: Any) -> Optional[int]:
    code = getattr(actor, "student_code", None)
    if actor.role != UserRole.STUDENT or not code:
        return None
    return session.execute(select(Student.id).where(Student.student_code == code)).scalars().first()


def _class_filter(session: Session, actor: Any):
    """The teaching assignments a staff member may see (None = all, for an admin)."""
    if actor.role == UserRole.ADMIN:
        return None
    clauses = [TeachingAssignment.faculty_id == actor.faculty_profile_id] if actor.faculty_profile_id else []
    if actor.role == UserRole.HOD:
        department = headed_department_id(session, actor.faculty_profile_id)
        if department is not None:
            clauses.append(TeachingAssignment.department_id == department)
    return or_(*clauses) if clauses else false()


def _visible_guardian_ids(session: Session, actor: Any, student_id: Optional[int]) -> List[Select]:
    """One id subquery per Guardian kind."""
    if actor.role == UserRole.STUDENT:
        if student_id is None:
            return []
        return [
            select(Assignment.guardian_mission_id).join(AssignmentTarget, AssignmentTarget.assignment_id == Assignment.id)
            .where(AssignmentTarget.student_id == student_id, Assignment.guardian_mission_id.is_not(None)),
            select(Exam.guardian_mission_id).join(ExamTarget, ExamTarget.exam_id == Exam.id)
            .where(ExamTarget.student_id == student_id, Exam.guardian_mission_id.is_not(None)),
            select(AttendanceIntervention.mission_id).where(AttendanceIntervention.student_id == student_id,
                                                            AttendanceIntervention.mission_id.is_not(None)),
        ]
    classes = _class_filter(session, actor)
    queries = [
        select(Assignment.guardian_mission_id).join(TeachingAssignment,
                                                    TeachingAssignment.id == Assignment.teaching_assignment_id),
        select(Exam.guardian_mission_id).join(TeachingAssignment, TeachingAssignment.id == Exam.teaching_assignment_id),
        select(AttendanceIntervention.mission_id).join(
            TeachingAssignment, TeachingAssignment.id == AttendanceIntervention.teaching_assignment_id),
    ]
    return [q.where(classes) if classes is not None else q for q in queries]


def _scope_clause(session: Session, actor: Any):
    if actor.role == UserRole.ADMIN:
        return AgentMission.agent_key.in_(AUTONOMOUS_AGENT_KEYS)
    student_id = _own_student_id(session, actor)
    guardians = _visible_guardian_ids(session, actor, student_id)
    clauses = [AgentMission.id.in_(q) for q in guardians]
    if actor.role == UserRole.STUDENT:
        if student_id is not None:
            clauses.append(AgentMission.id.in_(select(CommunicationJob.agent_mission_id).where(
                CommunicationJob.recipient_student_id == student_id)))
    else:
        clauses.append(AgentMission.owner_account_id == actor.account_id)
        if guardians:
            clauses.append(AgentMission.id.in_(select(CommunicationJob.agent_mission_id).where(
                or_(*[CommunicationJob.source_mission_id.in_(q) for q in guardians]))))
    return or_(*clauses) if clauses else false()


# --- Views ----------------------------------------------------------------------------------------------------------


def _course_code(session: Session, course_id: Optional[int]) -> Optional[str]:
    course = session.get(Course, course_id) if course_id else None
    return course.code if course is not None else None


def _count(session: Session, query: Select) -> int:
    return int(session.execute(select(func.count()).select_from(query.subquery())).scalar_one())


def _assignment(session: Session, mission: AgentMission, student_id: Optional[int]
                ) -> Optional[Tuple[MissionSubject, MissionProgress]]:
    row = session.execute(select(Assignment).where(Assignment.guardian_mission_id == mission.id)).scalars().first()
    if row is None:
        return None
    subject = MissionSubject(type="assignment", title=row.title, course_code=_course_code(session, row.course_id),
                             due_at=row.deadline_at)
    submitted = select(AssignmentSubmission.student_id).where(AssignmentSubmission.assignment_id == row.id)
    if student_id is not None:
        own = session.execute(select(AssignmentSubmission.status).where(
            AssignmentSubmission.assignment_id == row.id, AssignmentSubmission.student_id == student_id)).scalars().first()
        status = own.value if own is not None else ("cancelled" if row.status == AssignmentStatus.CANCELLED else "pending")
        return subject, MissionProgress(scope="self", own_status=status)
    total = _count(session, select(AssignmentTarget.id).where(AssignmentTarget.assignment_id == row.id))
    done = _count(session, submitted)
    return subject, MissionProgress(scope="class", total=total, resolved=done, pending=max(total - done, 0))


def _exam(session: Session, mission: AgentMission, student_id: Optional[int]
          ) -> Optional[Tuple[MissionSubject, MissionProgress]]:
    row = session.execute(select(Exam).where(Exam.guardian_mission_id == mission.id)).scalars().first()
    if row is None:
        return None
    code = _course_code(session, row.course_id)
    subject = MissionSubject(type="exam", title=row.title or f"{code or 'Course'} {row.exam_type}".strip(),
                             course_code=code, due_at=row.scheduled_start)
    if student_id is not None:
        own = session.execute(select(ExamAttendance.status).where(
            ExamAttendance.exam_id == row.id, ExamAttendance.student_id == student_id)).scalars().first()
        return subject, MissionProgress(scope="self", own_status=own.value if own is not None else "pending")
    total = _count(session, select(ExamTarget.id).where(ExamTarget.exam_id == row.id))
    done = _count(session, select(ExamAttendance.id).where(ExamAttendance.exam_id == row.id,
                                                           ExamAttendance.status.in_(_RESOLVED_EXAM)))
    return subject, MissionProgress(scope="class", total=total, resolved=done, pending=max(total - done, 0))


def _attendance(session: Session, mission: AgentMission, student_id: Optional[int]
                ) -> Optional[Tuple[MissionSubject, MissionProgress]]:
    row = session.execute(select(AttendanceIntervention).where(
        AttendanceIntervention.mission_id == mission.id)).scalars().first()
    if row is None or (student_id is not None and row.student_id != student_id):
        return None
    code = _course_code(session, row.course_id)
    subject = MissionSubject(type="attendance", title=f"{code or 'Class'} absence follow-up", course_code=code,
                             due_at=row.detected_at)
    return subject, MissionProgress(scope="self" if student_id is not None else "class", total=1,
                                    resolved=0 if row.status.value == "open" else 1,
                                    pending=1 if row.status.value == "open" else 0, own_status=row.status.value)


def _communication(session: Session, mission: AgentMission, student_id: Optional[int]
                   ) -> Optional[Tuple[MissionSubject, MissionProgress]]:
    job = session.execute(select(CommunicationJob).where(CommunicationJob.agent_mission_id == mission.id)).scalars().first()
    if job is None or (student_id is not None and job.recipient_student_id != student_id):
        return None
    purpose = job.purpose.replace("_", " ")
    subject = MissionSubject(type="communication", title=f"{job.source_type.capitalize()} {purpose}".strip())
    # Channel kind and status only; never the address it was sent to.
    return subject, MissionProgress(scope="self" if student_id is not None else "class", own_status=job.status.value)


_SUBJECTS = {ASSIGNMENT_GUARDIAN: _assignment, EXAM_GUARDIAN: _exam, ATTENDANCE_GUARDIAN: _attendance,
             COMMUNICATION_AGENT: _communication}


def _result_code(mission: AgentMission) -> Optional[str]:
    outcome = (mission.context or {}).get("outcome")
    code = outcome.get("result") if isinstance(outcome, dict) else None
    return code if isinstance(code, str) and _CODE.match(code) else None


def _last_wake(mission: AgentMission) -> Tuple[Optional[datetime], Optional[str]]:
    """When and why the mission last woke on its own (from its bounded observation memory; kinds and codes only)."""
    for observation in reversed(list((mission.context or {}).get("observations") or [])):
        if not isinstance(observation, dict) or observation.get("kind") not in ("wake", "event"):
            continue
        try:
            at = datetime.fromisoformat(str(observation.get("at")))
        except ValueError:
            return None, None
        source = str(observation.get("source") or "")
        trigger = "checkpoint" if observation.get("kind") == "wake" else (source if _CODE.match(source) else "event")
        return at, trigger
    return None, None


def _activity(session: Session, mission_id: int) -> List[MissionActivity]:
    steps = session.execute(select(AgentStep).where(AgentStep.mission_id == mission_id)
                            .order_by(AgentStep.step_number.desc()).limit(MAX_ACTIVITY)).scalars().all()
    return [MissionActivity(step_number=s.step_number, action_type=s.action_type, tool_name=s.tool_name,
                            status=s.status.value, created_at=s.created_at) for s in reversed(steps)]


def _view(session: Session, mission: AgentMission, student_id: Optional[int]) -> Optional[AutonomousMissionView]:
    resolved = _SUBJECTS[mission.agent_key](session, mission, student_id)
    if resolved is None:
        return None
    subject, progress = resolved
    staff = student_id is None
    woke_at, woke_by = _last_wake(mission)
    return AutonomousMissionView(
        last_wake_at=woke_at, last_wake_trigger=woke_by,
        mission_id=mission.id, agent_key=mission.agent_key, agent_label=AGENT_LABELS[mission.agent_key],
        kind="communication" if mission.agent_key == COMMUNICATION_AGENT else "guardian", status=mission.status,
        waiting_for=mission.waiting_for, next_wake_at=mission.next_wake_at, step_count=mission.step_count,
        created_at=mission.created_at, updated_at=mission.updated_at, completed_at=mission.completed_at,
        result_code=_result_code(mission) if staff else None, subject=subject, progress=progress,
        activity=_activity(session, mission.id) if staff else [])


def list_autonomous_missions(session: Session, actor: Any, *, limit: int = 20,
                             before_id: Optional[int] = None) -> AutonomousMissionPage:
    """Newest first, ``limit`` (1-50) per page; pass ``next_before_id`` back as ``before_id`` for the next page."""
    if actor.role not in (UserRole.STUDENT, UserRole.FACULTY, UserRole.HOD, UserRole.ADMIN):
        return AutonomousMissionPage(items=[], next_before_id=None)
    student_id = _own_student_id(session, actor) if actor.role == UserRole.STUDENT else None
    if actor.role == UserRole.STUDENT and student_id is None:
        return AutonomousMissionPage(items=[], next_before_id=None)
    limit = max(1, min(limit, MAX_LIMIT))
    query = select(AgentMission).where(AgentMission.organization_id == actor.organization_id,
                                       AgentMission.agent_key.in_(AUTONOMOUS_AGENT_KEYS), _scope_clause(session, actor))
    if before_id is not None:
        query = query.where(AgentMission.id < before_id)
    missions = session.execute(query.order_by(AgentMission.id.desc()).limit(limit + 1)).scalars().all()
    page, more = missions[:limit], len(missions) > limit
    items = [view for view in (_view(session, m, student_id) for m in page) if view is not None]
    return AutonomousMissionPage(items=items, next_before_id=page[-1].id if more and page else None)
