"""Managed exams (AgentOS V2 Phase 4): create, schedule, start, attendance, complete, cancel, progress, follow-ups.

Deterministic application service, the exam twin of ``app.services.assignments``. The caller is always the
server-resolved identity (a ``MissionActor`` or a ``ToolContext``) and the session is bound to its organization,
so another organization's exam behaves as missing.

* Faculty/HOD create exams only for classes they may teach (``assignments.may_teach``: their own class; HOD also
  any class of the department they head); an admin for any class of the organization. An exam is managed by its
  creator, the class's teaching faculty, the head of its department and admins; for anyone else it does not
  exist (404). Students see only exams they are targeted by (never drafts).
* Scheduling is one transaction: claim DRAFT -> SCHEDULED with a conditional UPDATE (a duplicate or concurrent
  schedule creates nothing twice), snapshot the roster into ``exam_targets``, create the one Exam Guardian mission,
  emit EXAM_SCHEDULED and audit.
* Attendance is recorded only here, only by a managing staff member, only for targeted students, under
  ``exam_rules.can_mark`` (idempotent: the same value again changes nothing). Each change emits
  EXAM_ATTENDANCE_MARKED (or MAKEUP_EXAM_COMPLETED) to the Guardian mission and wakes it; a resolved student's
  active follow-ups are closed at once.
* ``request_followups`` is the only way an ``ExamFollowup`` is created: each request is decided by
  ``evaluate_exam_followup`` (code), never by the model that asked for it.

Audit (``operation_audit_events``, subject ``exam``): EXAM_CREATED, EXAM_SCHEDULED, EXAM_MISSION_CREATED,
EXAM_STARTED, EXAM_ATTENDANCE_MARKED, EXAM_ENDED, EXAM_CANCELLED, EXAM_FOLLOWUP_REQUESTED, EXAM_FOLLOWUP_SUPPRESSED
(the Guardian adds EXAM_COMPLETED / EXAM_UNRESOLVED). Metadata: ids, counts, statuses and codes only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agentos.events import SUBJECT_MISSION, EventService, wake_mission
from app.agentos.schemas import CreateAgentMission, DomainEventType
from app.db.models.academic import Course, Exam, ExamStatus, ExamType
from app.db.models.agent_kernel import AgentMission
from app.db.models.assignment import FollowupStatus
from app.db.models.exam import ExamAttendance, ExamAttendanceStatus, ExamFollowup, ExamTarget
from app.db.models.faculty import TeachingAssignment
from app.db.repositories import operations_audit
from app.rules import exam_rules as rules
from app.schemas.enums import UserRole
from app.services.assignments import AssignmentError, headed_department_id, may_teach, own_student as _own_student
from app.services.class_schedule import roster

GUARDIAN_AGENT_KEY = "exam_guardian"
GUARDIAN_MAX_STEPS = 80
STAFF_ROLES = frozenset({UserRole.FACULTY, UserRole.HOD, UserRole.ADMIN})
SUBJECT = "exam"
AGENT_ROLE = "agent"
MAX_AHEAD = timedelta(days=366)
MAX_MARKS_PER_REQUEST = 200
MAX_FOLLOWUPS_PER_REQUEST = 200
DEFAULT_LOCATION = "To be announced"
_events = EventService()


class ExamError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


def _not_found() -> ExamError:
    return ExamError("EXAM_NOT_FOUND", "Exam not found.", 404)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("timestamps must include a timezone")
    return value


# --- Bodies and views ----------------------------------------------------------------------------------------------


class CreateExam(BaseModel):
    """Only the exam. Organization, creator and faculty come from the token (extra fields: 422)."""

    model_config = ConfigDict(extra="forbid")

    teaching_assignment_id: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    scheduled_at: datetime
    duration_minutes: int = Field(ge=rules.MIN_DURATION, le=rules.MAX_DURATION)
    exam_type: ExamType
    makeup_deadline_at: Optional[datetime] = None
    location: Optional[str] = Field(default=None, max_length=80)

    @field_validator("scheduled_at", "makeup_deadline_at")
    @classmethod
    def _timezone_required(cls, value: Optional[datetime]) -> Optional[datetime]:
        return _aware(value)


class AttendanceMarkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    student_code: str = Field(min_length=1, max_length=20)
    status: ExamAttendanceStatus
    reason_code: Optional[Literal[rules.REASON_CODES]] = None  # type: ignore[valid-type]


class MarkExamAttendance(BaseModel):
    """Marks for targeted students only. The marking staff member comes from the token."""

    model_config = ConfigDict(extra="forbid")

    marks: List[AttendanceMarkIn] = Field(min_length=1, max_length=MAX_MARKS_PER_REQUEST)


class ExamView(BaseModel):
    id: int
    title: Optional[str]
    exam_type: str
    status: Optional[ExamStatus]
    course_code: Optional[str]
    teaching_assignment_id: Optional[int]
    scheduled_at: datetime
    ends_at: datetime
    duration_minutes: Optional[int]
    location: str
    makeup_deadline_at: Optional[datetime]
    guardian_mission_id: Optional[int]
    created_at: Optional[datetime]


class ScheduleResult(BaseModel):
    exam: ExamView
    created: bool  # False: it was already scheduled (idempotent repeat)
    target_count: int
    guardian_mission_id: Optional[int]


class MarkResult(BaseModel):
    exam_id: int
    changed: int
    unchanged: int


class ExamStudentView(BaseModel):
    student_code: str
    attendance: Optional[ExamAttendanceStatus]
    absence_followups: int
    active_followup: bool


class ExamProgressView(BaseModel):
    exam_id: int
    status: Optional[ExamStatus]
    target_count: int
    present_count: int
    absent_count: int
    exempt_count: int
    makeup_completed_count: int
    unmarked_count: int
    resolved_count: int
    all_resolved: bool
    finished: bool
    terminal_deadline: datetime
    students: List[ExamStudentView]


class MyExamView(BaseModel):
    id: int
    title: Optional[str]
    exam_type: str
    course_code: Optional[str]
    status: Optional[ExamStatus]
    scheduled_at: datetime
    ends_at: datetime
    location: str
    makeup_deadline_at: Optional[datetime]
    my_attendance: Optional[ExamAttendanceStatus]


# --- Access ----------------------------------------------------------------------------------------------------------


def manageable(session: Session, actor: Any, exam_id: int) -> Exam:
    """The managed exam if the caller may manage it: admin, its creator, the class's faculty or the department head."""
    exam = session.get(Exam, exam_id) if actor.role in STAFF_ROLES else None
    if exam is None or exam.teaching_assignment_id is None:
        raise _not_found()
    if actor.role == UserRole.ADMIN or exam.created_by_account_id == actor.account_id:
        return exam
    teaching = session.get(TeachingAssignment, exam.teaching_assignment_id)
    if teaching is not None and actor.faculty_profile_id is not None and (
            teaching.faculty_id == actor.faculty_profile_id
            or (actor.role == UserRole.HOD
                and teaching.department_id == headed_department_id(session, actor.faculty_profile_id))):
        return exam
    raise _not_found()


def own_student(session: Session, actor: Any) -> Any:
    """The caller's own student record (from the token's membership), as an ``ExamError`` when there is none."""
    try:
        return _own_student(session, actor)
    except AssignmentError as exc:
        raise ExamError(exc.code, exc.message, exc.status_code) from None


def _target(session: Session, exam_id: int, student_id: int) -> Optional[ExamTarget]:
    return session.execute(select(ExamTarget).where(ExamTarget.exam_id == exam_id,
                                                    ExamTarget.student_id == student_id)).scalars().first()


def visible_to_student(session: Session, actor: Any, exam_id: int) -> Exam:
    student = own_student(session, actor)
    exam = session.get(Exam, exam_id)
    if exam is None or exam.status in (None, ExamStatus.DRAFT) or _target(session, exam_id, student.id) is None:
        raise _not_found()  # not targeted: indistinguishable from missing
    return exam


def _audit(session: Session, *, event_type: str, actor_account_id: Optional[int], actor_role: str, exam_id: int,
           message: str, now: datetime, **metadata: Any) -> None:
    operations_audit.record(session, event_type=event_type, actor_account_id=actor_account_id, actor_role=actor_role,
                            subject_type=SUBJECT, subject_id=str(exam_id), message=message, at=now,
                            metadata={"exam_id": exam_id, **metadata})


def view(session: Session, exam: Exam) -> ExamView:
    course = session.get(Course, exam.course_id)
    return ExamView(
        id=exam.id, title=exam.title, exam_type=exam.exam_type, status=exam.status,
        course_code=course.code if course else None, teaching_assignment_id=exam.teaching_assignment_id,
        scheduled_at=exam.scheduled_start, ends_at=exam.scheduled_end, duration_minutes=exam.duration_minutes,
        location=exam.location, makeup_deadline_at=exam.makeup_deadline_at,
        guardian_mission_id=exam.guardian_mission_id, created_at=exam.created_at,
    )


# --- Progress (deterministic; shared by the API, the Guardian and Nexus) ---------------------------------------------


@dataclass
class ExamProgress:
    target_ids: List[int]
    attendance: Dict[int, ExamAttendanceStatus] = field(default_factory=dict)

    @property
    def target_count(self) -> int:
        return len(self.target_ids)

    def ids_with(self, status: ExamAttendanceStatus) -> List[int]:
        return [sid for sid in self.target_ids if self.attendance.get(sid) == status]

    @property
    def unmarked_ids(self) -> List[int]:
        return [sid for sid in self.target_ids if sid not in self.attendance]

    @property
    def unresolved_ids(self) -> List[int]:
        return [sid for sid in self.target_ids if not rules.is_resolved(self.attendance.get(sid))]

    @property
    def resolved_count(self) -> int:
        return self.target_count - len(self.unresolved_ids)

    @property
    def all_resolved(self) -> bool:
        return rules.all_resolved(self.target_count, self.resolved_count)

    def counts(self) -> Dict[str, int]:
        return {"target_count": self.target_count,
                "present_count": len(self.ids_with(ExamAttendanceStatus.PRESENT)),
                "absent_count": len(self.ids_with(ExamAttendanceStatus.ABSENT)),
                "exempt_count": len(self.ids_with(ExamAttendanceStatus.EXEMPT)),
                "makeup_completed_count": len(self.ids_with(ExamAttendanceStatus.MAKEUP_COMPLETED)),
                "unmarked_count": len(self.unmarked_ids), "resolved_count": self.resolved_count}


def compute_progress(session: Session, exam: Exam) -> ExamProgress:
    target_ids = list(session.execute(select(ExamTarget.student_id).where(ExamTarget.exam_id == exam.id)
                                      .order_by(ExamTarget.student_id)).scalars())
    attendance = {sid: status for sid, status in session.execute(
        select(ExamAttendance.student_id, ExamAttendance.status).where(
            ExamAttendance.exam_id == exam.id, ExamAttendance.student_id.in_(target_ids or [-1])))}
    return ExamProgress(target_ids, attendance)


def terminal_deadline(exam: Exam, policy: rules.ExamPolicy) -> datetime:
    return rules.terminal_deadline(exam.scheduled_end, exam.makeup_deadline_at, policy)


@dataclass(frozen=True)
class FollowupState:
    attempts: int
    active: bool
    last_requested_at: Optional[datetime]


def followup_states(session: Session, exam_id: int, student_ids: Iterable[int], purpose: str) -> Dict[int, FollowupState]:
    ids = list(student_ids)
    rows = session.execute(select(ExamFollowup.student_id, ExamFollowup.status, ExamFollowup.created_at).where(
        ExamFollowup.exam_id == exam_id, ExamFollowup.purpose == purpose, ExamFollowup.student_id.in_(ids or [-1]))).all()
    out: Dict[int, FollowupState] = {sid: FollowupState(0, False, None) for sid in ids}
    for sid, status, created_at in rows:
        prev = out[sid]
        last = created_at if prev.last_requested_at is None or created_at > prev.last_requested_at else prev.last_requested_at
        out[sid] = FollowupState(prev.attempts + 1, prev.active or status == FollowupStatus.REQUESTED, last)
    return out


def progress_view(session: Session, exam: Exam, now: datetime, policy: rules.ExamPolicy) -> ExamProgressView:
    progress = compute_progress(session, exam)
    states = followup_states(session, exam.id, progress.target_ids, rules.PURPOSE_ABSENCE)
    codes = dict(session.execute(select(ExamTarget.student_id, ExamTarget.student_code).where(
        ExamTarget.exam_id == exam.id)).all())
    return ExamProgressView(
        exam_id=exam.id, status=exam.status, **progress.counts(), all_resolved=progress.all_resolved,
        finished=rules.finished(exam.status, exam.scheduled_end, now), terminal_deadline=terminal_deadline(exam, policy),
        students=[ExamStudentView(student_code=codes[sid], attendance=progress.attendance.get(sid),
                                  absence_followups=states[sid].attempts, active_followup=states[sid].active)
                  for sid in progress.target_ids])


# --- Staff operations ----------------------------------------------------------------------------------------------


def create_exam(session: Session, actor: Any, body: CreateExam, now: datetime) -> Exam:
    if actor.role not in STAFF_ROLES:
        raise ExamError("ROLE_NOT_ALLOWED", "Only faculty, a head of department or an admin can create exams.", 403)
    teaching = session.get(TeachingAssignment, body.teaching_assignment_id)
    if teaching is None or not may_teach(session, actor, teaching):
        raise ExamError("CLASS_NOT_FOUND", "Class not found among the classes you may schedule exams for.", 404)
    if not now < body.scheduled_at <= now + MAX_AHEAD:
        raise ExamError("INVALID_SCHEDULE", "The exam must be scheduled in the future (within a year).", 422)
    end = rules.exam_end(body.scheduled_at, body.duration_minutes)
    if body.makeup_deadline_at is not None and body.makeup_deadline_at <= end:
        raise ExamError("INVALID_MAKEUP_DEADLINE", "The makeup deadline must be after the exam ends.", 422)
    exam = Exam(
        course_id=teaching.course_id, teaching_assignment_id=teaching.id, created_by_account_id=actor.account_id,
        title=body.title.strip(), exam_type=body.exam_type.value, scheduled_start=body.scheduled_at, scheduled_end=end,
        duration_minutes=body.duration_minutes, location=(body.location or "").strip() or DEFAULT_LOCATION,
        makeup_deadline_at=body.makeup_deadline_at, status=ExamStatus.DRAFT, created_at=now, updated_at=now,
    )
    session.add(exam)
    session.flush()
    _audit(session, event_type="EXAM_CREATED", actor_account_id=actor.account_id, actor_role=actor.role.value,
           exam_id=exam.id, message=f"Exam {exam.id} created.", now=now, teaching_assignment_id=teaching.id,
           course_id=teaching.course_id, exam_type=exam.exam_type)
    session.commit()
    return exam


def _claim(session: Session, exam_id: int, from_status: ExamStatus, values: Dict[str, Any]) -> bool:
    """Atomic status transition: of two concurrent requests only one updates the row."""
    return session.execute(update(Exam).where(Exam.id == exam_id, Exam.status == from_status).values(**values)
                           .execution_options(synchronize_session=False)).rowcount == 1


def schedule(session: Session, runtime: Any, actor: Any, exam_id: int, now: datetime) -> ScheduleResult:
    exam = manageable(session, actor, exam_id)
    if exam.status == ExamStatus.SCHEDULED or (exam.status in (ExamStatus.IN_PROGRESS, ExamStatus.COMPLETED)
                                               and exam.guardian_mission_id is not None):
        return _schedule_result(session, exam, created=False)
    if exam.status != ExamStatus.DRAFT:
        raise ExamError("EXAM_NOT_DRAFT", f"Exam is {exam.status.value if exam.status else 'unmanaged'}.", 409)
    if exam.scheduled_start <= now:
        raise ExamError("INVALID_SCHEDULE", "The exam time has already passed.", 409)
    if not _claim(session, exam.id, ExamStatus.DRAFT, {"status": ExamStatus.SCHEDULED, "published_at": now,
                                                        "updated_at": now}):
        session.rollback()
        exam = manageable(session, actor, exam_id)
        if exam.status == ExamStatus.SCHEDULED:
            return _schedule_result(session, exam, created=False)
        raise ExamError("EXAM_NOT_DRAFT", "Exam is no longer a draft.", 409)
    session.refresh(exam)
    students = roster(session, session.get(TeachingAssignment, exam.teaching_assignment_id))
    if not students:
        session.rollback()
        raise ExamError("NO_TARGET_STUDENTS", "No students are enrolled in this class.", 409)
    session.add_all([ExamTarget(exam_id=exam.id, student_id=s.id, student_code=s.student_code, section=s.section,
                                created_at=now) for s in students])
    mission = runtime.create_mission(session, actor, CreateAgentMission(
        agent_key=GUARDIAN_AGENT_KEY, max_steps=GUARDIAN_MAX_STEPS, context={"exam_id": exam.id},
        goal=(f"Ensure every targeted student of exam {exam.id} takes it (or is exempt or completes a makeup), "
              "while following institution communication policy and deadlines."),
        success_criteria=["The exam has finished and every target is present, exempt or makeup-completed "
                          "(verified from attendance records).",
                          "Reminders and follow-ups are requested only when the communication policy allows them."],
    ), internal=True, commit=False, next_wake_at=now)
    exam.guardian_mission_id = mission.id
    _events.publish(session, event_type=DomainEventType.EXAM_SCHEDULED, actor_account_id=actor.account_id,
                    subject_type=SUBJECT, subject_id=exam.id, now=now,
                    payload={"exam_id": exam.id, "target_count": len(students), "mission_id": mission.id,
                             "scheduled_at": exam.scheduled_start.isoformat()})
    _audit(session, event_type="EXAM_SCHEDULED", actor_account_id=actor.account_id, actor_role=actor.role.value,
           exam_id=exam.id, message=f"Exam {exam.id} scheduled for {len(students)} students.", now=now,
           target_count=len(students))
    _audit(session, event_type="EXAM_MISSION_CREATED", actor_account_id=actor.account_id, actor_role=actor.role.value,
           exam_id=exam.id, now=now, mission_id=mission.id,
           message=f"Exam Guardian mission {mission.id} created for exam {exam.id}.")
    session.commit()
    return _schedule_result(session, exam, created=True)


def _schedule_result(session: Session, exam: Exam, *, created: bool) -> ScheduleResult:
    count = session.execute(select(func.count(ExamTarget.id)).where(ExamTarget.exam_id == exam.id)).scalar_one()
    return ScheduleResult(exam=view(session, exam), created=created, target_count=count,
                          guardian_mission_id=exam.guardian_mission_id)


def _signal(session: Session, exam: Exam, event_type: DomainEventType, actor_account_id: int, now: datetime,
            **payload: Any) -> None:
    """A domain event addressed to the Guardian mission, plus an early wake."""
    if exam.guardian_mission_id is None:
        return
    _events.publish(session, event_type=event_type, actor_account_id=actor_account_id, subject_type=SUBJECT_MISSION,
                    subject_id=exam.guardian_mission_id, now=now, payload={"exam_id": exam.id, **payload})
    wake_mission(session, exam.guardian_mission_id, now)


def start(session: Session, actor: Any, exam_id: int, now: datetime) -> Exam:
    exam = manageable(session, actor, exam_id)
    if exam.status == ExamStatus.IN_PROGRESS:
        return exam
    decision = rules.can_start(exam.status, exam.scheduled_start, exam.scheduled_end, now)
    if not decision.allowed:
        raise ExamError(decision.code, "The exam cannot be started now.", 409)
    if not _claim(session, exam.id, ExamStatus.SCHEDULED, {"status": ExamStatus.IN_PROGRESS, "started_at": now,
                                                            "updated_at": now}):
        session.rollback()
        raise ExamError("EXAM_NOT_SCHEDULED", "The exam is no longer scheduled.", 409)
    session.refresh(exam)
    closed = close_active_followups(session, exam.id, now=now, purpose=rules.PURPOSE_REMINDER)  # reminders are moot now
    _signal(session, exam, DomainEventType.EXAM_STARTED, actor.account_id, now)
    _audit(session, event_type="EXAM_STARTED", actor_account_id=actor.account_id, actor_role=actor.role.value,
           exam_id=exam.id, message=f"Exam {exam.id} started.", now=now, reminders_closed=closed)
    session.commit()
    return exam


def complete(session: Session, actor: Any, exam_id: int, now: datetime) -> Exam:
    exam = manageable(session, actor, exam_id)
    if exam.status == ExamStatus.COMPLETED:
        return exam
    decision = rules.can_complete(exam.status)
    if not decision.allowed or not _claim(session, exam.id, ExamStatus.IN_PROGRESS, {
            "status": ExamStatus.COMPLETED, "completed_at": now, "updated_at": now}):
        session.rollback()
        raise ExamError("EXAM_NOT_IN_PROGRESS", "Only an exam in progress can be completed.", 409)
    session.refresh(exam)
    _signal(session, exam, DomainEventType.EXAM_ENDED, actor.account_id, now)
    _audit(session, event_type="EXAM_ENDED", actor_account_id=actor.account_id, actor_role=actor.role.value,
           exam_id=exam.id, message=f"Exam {exam.id} sitting completed.", now=now)
    session.commit()
    return exam


def cancel(session: Session, actor: Any, exam_id: int, now: datetime) -> Exam:
    exam = manageable(session, actor, exam_id)
    if exam.status == ExamStatus.CANCELLED:
        return exam
    if not rules.can_cancel(exam.status).allowed:
        raise ExamError("EXAM_NOT_CANCELLABLE", f"Exam is {exam.status.value if exam.status else 'unmanaged'}.", 409)
    exam.status, exam.cancelled_at, exam.updated_at = ExamStatus.CANCELLED, now, now
    closed = close_active_followups(session, exam.id, now=now)
    _signal(session, exam, DomainEventType.EXAM_CANCELLED, actor.account_id, now)
    _audit(session, event_type="EXAM_CANCELLED", actor_account_id=actor.account_id, actor_role=actor.role.value,
           exam_id=exam.id, message=f"Exam {exam.id} cancelled.", now=now, followups_closed=closed)
    session.commit()
    return exam


def mark_attendance(session: Session, actor: Any, exam_id: int, body: MarkExamAttendance, now: datetime) -> MarkResult:
    """All-or-nothing: every code must be a target and every change allowed, or nothing is written."""
    exam = manageable(session, actor, exam_id)
    targets = dict(session.execute(select(ExamTarget.student_code, ExamTarget.student_id).where(
        ExamTarget.exam_id == exam.id)).all())
    wanted: Dict[int, AttendanceMarkIn] = {}
    for mark in body.marks:
        sid = targets.get(mark.student_code)
        if sid is None:
            raise ExamError("NOT_A_TARGET", "A student in this request is not a target of this exam.", 422)
        if sid in wanted and wanted[sid].status != mark.status:
            raise ExamError("CONFLICTING_MARKS", "A student appears twice with different statuses.", 422)
        wanted[sid] = mark
    existing = {row.student_id: row for row in session.execute(select(ExamAttendance).where(
        ExamAttendance.exam_id == exam.id, ExamAttendance.student_id.in_(list(wanted)))).scalars()}
    changes: List[tuple] = []
    for sid, mark in wanted.items():
        current = existing[sid].status if sid in existing else None
        decision = rules.can_mark(exam.status, current, mark.status)
        if not decision.allowed:
            raise ExamError(decision.code, "This attendance change is not allowed.", 409)
        if decision.code != "UNCHANGED":
            changes.append((sid, current, mark))
    for sid, current, mark in changes:
        row = existing.get(sid)
        if row is None:
            session.add(ExamAttendance(exam_id=exam.id, student_id=sid, status=mark.status, marked_at=now,
                                       marked_by_account_id=actor.account_id, reason_code=mark.reason_code,
                                       created_at=now, updated_at=now))
        else:
            row.status, row.marked_at, row.marked_by_account_id = mark.status, now, actor.account_id
            row.reason_code, row.updated_at = mark.reason_code, now
    try:
        session.flush()
    except IntegrityError:  # a concurrent marking inserted the same (exam, student) first
        session.rollback()
        raise ExamError("CONCURRENT_UPDATE", "Attendance changed concurrently; reload and retry.", 409) from None
    for sid, current, mark in changes:
        if rules.is_resolved(mark.status):
            close_active_followups(session, exam.id, now=now, student_id=sid)
        kind = (DomainEventType.MAKEUP_EXAM_COMPLETED if mark.status == ExamAttendanceStatus.MAKEUP_COMPLETED
                else DomainEventType.EXAM_ATTENDANCE_MARKED)
        _signal(session, exam, kind, actor.account_id, now, student_id=sid, status=mark.status.value,
                previous=current.value if current else None)
    if changes:
        by_status: Dict[str, int] = {}
        for _, _, mark in changes:
            by_status[mark.status.value] = by_status.get(mark.status.value, 0) + 1
        _audit(session, event_type="EXAM_ATTENDANCE_MARKED", actor_account_id=actor.account_id,
               actor_role=actor.role.value, exam_id=exam.id, now=now, changed=len(changes), by_status=by_status,
               student_ids=[sid for sid, _, _ in changes][:50],
               message=f"Exam {exam.id}: attendance recorded for {len(changes)} student(s).")
    session.commit()
    return MarkResult(exam_id=exam.id, changed=len(changes), unchanged=len(wanted) - len(changes))


# --- Student operations ----------------------------------------------------------------------------------------------


def my_exams(session: Session, actor: Any) -> List[MyExamView]:
    student = own_student(session, actor)
    rows = session.execute(
        select(Exam, Course.code, ExamAttendance.status)
        .join(ExamTarget, ExamTarget.exam_id == Exam.id)
        .join(Course, Course.id == Exam.course_id)
        .outerjoin(ExamAttendance, (ExamAttendance.exam_id == Exam.id) & (ExamAttendance.student_id == student.id))
        .where(ExamTarget.student_id == student.id, Exam.status.is_not(None), Exam.status != ExamStatus.DRAFT)
        .order_by(Exam.scheduled_start, Exam.id)
    ).all()
    return [MyExamView(id=e.id, title=e.title, exam_type=e.exam_type, course_code=code, status=e.status,
                       scheduled_at=e.scheduled_start, ends_at=e.scheduled_end, location=e.location,
                       makeup_deadline_at=e.makeup_deadline_at, my_attendance=status) for e, code, status in rows]


# --- Guardian support ------------------------------------------------------------------------------------------------


def close_active_followups(session: Session, exam_id: int, *, now: datetime, student_id: Optional[int] = None,
                           purpose: Optional[str] = None) -> int:
    query = select(ExamFollowup).where(ExamFollowup.exam_id == exam_id, ExamFollowup.status == FollowupStatus.REQUESTED)
    if student_id is not None:
        query = query.where(ExamFollowup.student_id == student_id)
    if purpose is not None:
        query = query.where(ExamFollowup.purpose == purpose)
    rows = list(session.execute(query).scalars())
    for row in rows:
        row.status, row.updated_at = FollowupStatus.CANCELLED, now
    return len(rows)


def request_followups(
    session: Session, *, mission: AgentMission, exam: Exam, student_ids: Optional[Iterable[int]], purpose: str,
    preferred_channel: Optional[str], policy: rules.ExamPolicy, now: datetime, actor_account_id: int,
) -> List[Dict[str, Any]]:
    """Decide each requested follow-up in code. ``student_ids`` None = every target (reminder) or every confirmed
    absentee (absence follow-up). Allowed ones become an ExamFollowup plus a COMMUNICATION_REQUESTED event."""
    progress = compute_progress(session, exam)
    if student_ids is None:
        student_ids = (progress.target_ids if purpose == rules.PURPOSE_REMINDER
                       else progress.ids_with(ExamAttendanceStatus.ABSENT))
    ids = list(dict.fromkeys(int(s) for s in student_ids))[:MAX_FOLLOWUPS_PER_REQUEST]
    targets = set(progress.target_ids)
    states = followup_states(session, exam.id, ids, purpose)
    terminal = terminal_deadline(exam, policy)
    results: List[Dict[str, Any]] = []
    for sid in ids:
        state = states[sid]
        decision = rules.evaluate_exam_followup(
            now=now, purpose=purpose, exam_status=exam.status, start=exam.scheduled_start, terminal=terminal,
            is_target=sid in targets, attendance=progress.attendance.get(sid), active_followup=state.active,
            attempts_so_far=state.attempts, last_requested_at=state.last_requested_at, policy=policy)
        if not decision.allowed:
            _audit(session, event_type="EXAM_FOLLOWUP_SUPPRESSED", actor_account_id=actor_account_id,
                   actor_role=AGENT_ROLE, exam_id=exam.id, now=now, mission_id=mission.id, student_id=sid,
                   purpose=purpose, reason=decision.reason,
                   message=f"Exam {exam.id}: follow-up suppressed ({decision.reason}).")
            results.append({"student_id": sid, "result": "suppressed", "reason": decision.reason})
            continue
        followup = ExamFollowup(
            exam_id=exam.id, student_id=sid, mission_id=mission.id, channel_requested=preferred_channel,
            purpose=purpose, urgency=decision.urgency, status=FollowupStatus.REQUESTED,
            attempt_number=decision.attempt_number, reason=decision.reason, not_before_at=decision.not_before,
            created_at=now, updated_at=now)
        session.add(followup)
        session.flush()
        _events.publish(session, event_type=DomainEventType.COMMUNICATION_REQUESTED, actor_account_id=actor_account_id,
                        subject_type="exam_followup", subject_id=followup.id, now=now, payload={
                            "mission_id": mission.id, "exam_id": exam.id, "student_id": sid, "followup_id": followup.id,
                            "purpose": purpose, "urgency": decision.urgency, "preferred_channel": preferred_channel,
                            "not_before": decision.not_before.isoformat() if decision.not_before else None})
        _audit(session, event_type="EXAM_FOLLOWUP_REQUESTED", actor_account_id=actor_account_id,
               actor_role=AGENT_ROLE, exam_id=exam.id, now=now, mission_id=mission.id, student_id=sid,
               followup_id=followup.id, purpose=purpose, attempt_number=decision.attempt_number,
               urgency=decision.urgency, message=f"Exam {exam.id}: follow-up {followup.id} requested.")
        results.append({"student_id": sid, "result": "requested", "attempt_number": decision.attempt_number,
                        "urgency": decision.urgency, "deferred_for_quiet_hours": decision.not_before is not None})
    return results
