"""Assignments (AgentOS V2 Phase 3): create, publish, cancel, submit, progress, follow-up requests.

Deterministic application service. The caller (``organization_id``, ``account_id``, ``role``,
``faculty_profile_id``, ``student_code``) is always the server-resolved identity -- a
``MissionActor`` or a ``ToolContext`` -- and the session is already bound to that
organization, so a row of another organization behaves as missing.

* Faculty/HOD create assignments only for their own classes (HOD: also any class of the
  department they head); an admin for any class of the organization. An assignment is
  visible to, and managed by, its creator, the head of its department and admins; to
  anyone else it does not exist (404).
* Publishing is one transaction: claim DRAFT -> PUBLISHED with a conditional UPDATE (so a
  duplicate or concurrent publish creates nothing twice), snapshot the roster, create the
  one Assignment Guardian mission, emit ASSIGNMENT_PUBLISHED and audit.
* A student submits only for themself (student from the token), only when targeted.
  On time / LATE comes from ``app.rules.assignment_rules``. The submission emits
  ASSIGNMENT_SUBMITTED to the Guardian mission and wakes it; it never completes the mission.
* ``request_followups`` is the only way a follow-up row is created: every request is
  decided by ``evaluate_followup`` (code), never by the model that asked for it.

Audit (``operation_audit_events``, subject ``assignment``): ASSIGNMENT_CREATED, ASSIGNMENT_PUBLISHED,
ASSIGNMENT_MISSION_CREATED, ASSIGNMENT_CANCELLED, ASSIGNMENT_SUBMITTED, ASSIGNMENT_FOLLOWUP_REQUESTED,
ASSIGNMENT_FOLLOWUP_SUPPRESSED (the Guardian adds ASSIGNMENT_COMPLETED / ASSIGNMENT_DEADLINE_REACHED).
Metadata holds ids, counts, statuses and reason codes only -- never titles, submission text or contact details.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agentos.events import SUBJECT_MISSION, EventService, wake_mission as events_wake_mission
from app.agentos.schemas import CreateAgentMission, DomainEventType
from app.db.models.academic import Course
from app.db.models.agent_kernel import AgentMission
from app.db.models.assignment import (
    Assignment, AssignmentFollowup, AssignmentStatus, AssignmentSubmission, AssignmentTarget, FollowupStatus,
    SubmissionStatus,
)
from app.db.models.faculty import TeachingAssignment
from app.db.models.identity import Department, Student
from app.db.repositories import operations_audit
from app.rules import assignment_rules as rules
from app.schemas.enums import UserRole
from app.services.class_schedule import roster

GUARDIAN_AGENT_KEY = "assignment_guardian"
GUARDIAN_MAX_STEPS = 60
STAFF_ROLES = frozenset({UserRole.FACULTY, UserRole.HOD, UserRole.ADMIN})
SUBJECT = "assignment"
AGENT_ROLE = "agent"
MAX_DEADLINE_AHEAD = timedelta(days=366)
MAX_FOLLOWUPS_PER_REQUEST = 50
_events = EventService()


class AssignmentError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


def _not_found() -> AssignmentError:
    return AssignmentError("ASSIGNMENT_NOT_FOUND", "Assignment not found.", 404)


# --- Bodies and views ----------------------------------------------------------------------------------------------


class CreateAssignment(BaseModel):
    """Only the assignment. Organization, creator and faculty come from the token (extra fields: 422)."""

    model_config = ConfigDict(extra="forbid")

    teaching_assignment_id: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    deadline_at: datetime

    @field_validator("deadline_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline_at must include a timezone")
        return value


class SubmitAssignment(BaseModel):
    """Only the answer. The student comes from the token; there is no student field to fill in."""

    model_config = ConfigDict(extra="forbid")

    content_text: Optional[str] = Field(default=None, max_length=4000)


class AssignmentView(BaseModel):
    id: int
    title: str
    description: str
    status: AssignmentStatus
    course_code: Optional[str]
    teaching_assignment_id: int
    deadline_at: datetime
    published_at: Optional[datetime]
    guardian_mission_id: Optional[int]
    created_at: datetime


class PublishResult(BaseModel):
    assignment: AssignmentView
    created: bool  # False: it was already published (idempotent repeat)
    target_count: int
    guardian_mission_id: Optional[int]


class PendingStudentView(BaseModel):
    student_code: str
    followups_requested: int
    active_followup: bool


class ProgressView(BaseModel):
    assignment_id: int
    status: AssignmentStatus
    target_count: int
    submitted_count: int
    on_time_count: int
    late_count: int
    pending_count: int
    all_submitted: bool
    deadline_passed: bool
    pending: List[PendingStudentView]


class MyAssignmentView(BaseModel):
    id: int
    title: str
    description: str
    course_code: Optional[str]
    status: AssignmentStatus
    deadline_at: datetime
    submission_status: Optional[SubmissionStatus]
    submitted_at: Optional[datetime]


class SubmissionView(BaseModel):
    assignment_id: int
    status: SubmissionStatus
    submitted_at: datetime


# --- Access --------------------------------------------------------------------------------------------------------


def headed_department_id(session: Session, faculty_profile_id: Optional[int]) -> Optional[int]:
    if faculty_profile_id is None:
        return None
    return session.execute(select(Department.id).where(Department.hod_faculty_id == faculty_profile_id)).scalars().first()


def may_teach(session: Session, actor: Any, teaching: TeachingAssignment) -> bool:
    if actor.role == UserRole.ADMIN:
        return True
    if actor.role not in (UserRole.FACULTY, UserRole.HOD) or actor.faculty_profile_id is None:
        return False
    if teaching.faculty_id == actor.faculty_profile_id:
        return True
    return actor.role == UserRole.HOD and teaching.department_id == headed_department_id(session, actor.faculty_profile_id)


def manageable(session: Session, actor: Any, assignment_id: int) -> Assignment:
    """The assignment if the caller may manage/inspect it: admin, its creator, or the head of its department."""
    assignment = session.get(Assignment, assignment_id) if actor.role in STAFF_ROLES else None
    if assignment is None:
        raise _not_found()
    if actor.role == UserRole.ADMIN or assignment.created_by_account_id == actor.account_id:
        return assignment
    teaching = session.get(TeachingAssignment, assignment.teaching_assignment_id)
    if (actor.role == UserRole.HOD and teaching is not None
            and teaching.department_id == headed_department_id(session, actor.faculty_profile_id)):
        return assignment
    raise _not_found()


def own_student(session: Session, actor: Any) -> Student:
    student_code = getattr(actor, "student_code", None)
    student = (session.execute(select(Student).where(Student.student_code == student_code)).scalars().first()
               if actor.role == UserRole.STUDENT and student_code else None)
    if student is None:
        raise AssignmentError("NOT_A_STUDENT", "This account is not linked to a student record.", 403)
    return student


def _target(session: Session, assignment_id: int, student_id: int) -> Optional[AssignmentTarget]:
    return session.execute(select(AssignmentTarget).where(
        AssignmentTarget.assignment_id == assignment_id, AssignmentTarget.student_id == student_id)).scalars().first()


def _audit(session: Session, *, event_type: str, actor_account_id: Optional[int], actor_role: str, assignment_id: int,
           message: str, now: datetime, **metadata: Any) -> None:
    operations_audit.record(session, event_type=event_type, actor_account_id=actor_account_id, actor_role=actor_role,
                            subject_type=SUBJECT, subject_id=str(assignment_id), message=message, at=now,
                            metadata={"assignment_id": assignment_id, **metadata})


def view(session: Session, assignment: Assignment) -> AssignmentView:
    course = session.get(Course, assignment.course_id)
    return AssignmentView(
        id=assignment.id, title=assignment.title, description=assignment.description, status=assignment.status,
        course_code=course.code if course else None, teaching_assignment_id=assignment.teaching_assignment_id,
        deadline_at=assignment.deadline_at, published_at=assignment.published_at,
        guardian_mission_id=assignment.guardian_mission_id, created_at=assignment.created_at,
    )


# --- Progress (deterministic; shared by the API, the Guardian and Nexus) -------------------------------------------


@dataclass
class Progress:
    target_ids: List[int]
    submitted: Dict[int, SubmissionStatus]
    pending_ids: List[int] = field(default_factory=list)

    @property
    def target_count(self) -> int:
        return len(self.target_ids)

    @property
    def submitted_count(self) -> int:
        return len(self.submitted)

    @property
    def late_count(self) -> int:
        return sum(1 for s in self.submitted.values() if s == SubmissionStatus.LATE)

    @property
    def all_submitted(self) -> bool:
        return rules.all_targets_submitted(self.target_count, self.submitted_count)


def compute_progress(session: Session, assignment: Assignment) -> Progress:
    target_ids = list(session.execute(select(AssignmentTarget.student_id).where(
        AssignmentTarget.assignment_id == assignment.id).order_by(AssignmentTarget.student_id)).scalars())
    submitted = {sid: status for sid, status in session.execute(select(
        AssignmentSubmission.student_id, AssignmentSubmission.status).where(
        AssignmentSubmission.assignment_id == assignment.id, AssignmentSubmission.student_id.in_(target_ids or [-1])))}
    return Progress(target_ids, submitted, [sid for sid in target_ids if sid not in submitted])


@dataclass(frozen=True)
class FollowupState:
    attempts: int
    active: bool
    last_requested_at: Optional[datetime]


def followup_states(session: Session, assignment_id: int, student_ids: Iterable[int]) -> Dict[int, FollowupState]:
    ids = list(student_ids)
    rows = session.execute(select(AssignmentFollowup.student_id, AssignmentFollowup.status, AssignmentFollowup.created_at)
                           .where(AssignmentFollowup.assignment_id == assignment_id,
                                  AssignmentFollowup.student_id.in_(ids or [-1]))).all()
    out: Dict[int, FollowupState] = {sid: FollowupState(0, False, None) for sid in ids}
    for sid, status, created_at in rows:
        prev = out[sid]
        last = created_at if prev.last_requested_at is None or created_at > prev.last_requested_at else prev.last_requested_at
        out[sid] = FollowupState(prev.attempts + 1, prev.active or status == FollowupStatus.REQUESTED, last)
    return out


def progress_view(session: Session, assignment: Assignment, now: datetime) -> ProgressView:
    progress = compute_progress(session, assignment)
    states = followup_states(session, assignment.id, progress.pending_ids)
    codes = dict(session.execute(select(AssignmentTarget.student_id, AssignmentTarget.student_code).where(
        AssignmentTarget.assignment_id == assignment.id)).all())
    return ProgressView(
        assignment_id=assignment.id, status=assignment.status, target_count=progress.target_count,
        submitted_count=progress.submitted_count, on_time_count=progress.submitted_count - progress.late_count,
        late_count=progress.late_count, pending_count=len(progress.pending_ids), all_submitted=progress.all_submitted,
        deadline_passed=rules.deadline_passed(assignment.deadline_at, now),
        pending=[PendingStudentView(student_code=codes[sid], followups_requested=states[sid].attempts,
                                    active_followup=states[sid].active) for sid in progress.pending_ids],
    )


# --- Faculty operations --------------------------------------------------------------------------------------------


def create_assignment(session: Session, actor: Any, body: CreateAssignment, now: datetime) -> Assignment:
    if actor.role not in STAFF_ROLES:
        raise AssignmentError("ROLE_NOT_ALLOWED", "Only faculty, a head of department or an admin can create assignments.", 403)
    teaching = session.get(TeachingAssignment, body.teaching_assignment_id)
    if teaching is None or not may_teach(session, actor, teaching):
        raise AssignmentError("CLASS_NOT_FOUND", "Class not found among the classes you may assign work to.", 404)
    if not now < body.deadline_at <= now + MAX_DEADLINE_AHEAD:
        raise AssignmentError("INVALID_DEADLINE", "The deadline must be in the future (within a year).", 422)
    assignment = Assignment(
        created_by_account_id=actor.account_id, teaching_assignment_id=teaching.id, course_id=teaching.course_id,
        created_by_faculty_id=actor.faculty_profile_id if actor.role in (UserRole.FACULTY, UserRole.HOD) else None,
        title=body.title.strip(), description=body.description, deadline_at=body.deadline_at,
        status=AssignmentStatus.DRAFT, created_at=now, updated_at=now,
    )
    session.add(assignment)
    session.flush()
    _audit(session, event_type="ASSIGNMENT_CREATED", actor_account_id=actor.account_id, actor_role=actor.role.value,
           assignment_id=assignment.id, message=f"Assignment {assignment.id} created.", now=now,
           teaching_assignment_id=teaching.id, course_id=teaching.course_id)
    session.commit()
    return assignment


def publish(session: Session, runtime: Any, actor: Any, assignment_id: int, now: datetime) -> PublishResult:
    assignment = manageable(session, actor, assignment_id)
    if assignment.status == AssignmentStatus.PUBLISHED:
        return _publish_result(session, assignment, created=False)
    if assignment.status != AssignmentStatus.DRAFT:
        raise AssignmentError("ASSIGNMENT_NOT_DRAFT", f"Assignment is {assignment.status.value}.", 409)
    if assignment.deadline_at <= now:
        raise AssignmentError("INVALID_DEADLINE", "The deadline has already passed.", 409)
    # Claim the transition atomically: of two concurrent publishes only one updates the row.
    claimed = session.execute(
        update(Assignment).where(Assignment.id == assignment.id, Assignment.status == AssignmentStatus.DRAFT)
        .values(status=AssignmentStatus.PUBLISHED, published_at=now, updated_at=now)
        .execution_options(synchronize_session=False)).rowcount
    if claimed != 1:
        session.rollback()
        assignment = manageable(session, actor, assignment_id)
        if assignment.status == AssignmentStatus.PUBLISHED:
            return _publish_result(session, assignment, created=False)
        raise AssignmentError("ASSIGNMENT_NOT_DRAFT", f"Assignment is {assignment.status.value}.", 409)
    session.refresh(assignment)
    students = roster(session, session.get(TeachingAssignment, assignment.teaching_assignment_id))
    if not students:
        session.rollback()
        raise AssignmentError("NO_TARGET_STUDENTS", "No students are enrolled in this class.", 409)
    session.add_all([AssignmentTarget(assignment_id=assignment.id, student_id=s.id, student_code=s.student_code,
                                      section=s.section, created_at=now) for s in students])
    mission = runtime.create_mission(session, actor, CreateAgentMission(
        agent_key=GUARDIAN_AGENT_KEY, max_steps=GUARDIAN_MAX_STEPS, context={"assignment_id": assignment.id},
        goal=(f"Ensure every targeted student completes assignment {assignment.id}, while following institution "
              "communication policy and deadline constraints."),
        success_criteria=["Every targeted student has submitted (verified from submissions).",
                          "Follow-ups are requested only when the communication policy allows them."],
    ), internal=True, commit=False, next_wake_at=now)
    assignment.guardian_mission_id = mission.id
    _events.publish(session, event_type=DomainEventType.ASSIGNMENT_PUBLISHED, actor_account_id=actor.account_id,
                    subject_type=SUBJECT, subject_id=assignment.id, now=now,
                    payload={"assignment_id": assignment.id, "target_count": len(students),
                             "deadline_at": assignment.deadline_at.isoformat(), "mission_id": mission.id})
    _audit(session, event_type="ASSIGNMENT_PUBLISHED", actor_account_id=actor.account_id, actor_role=actor.role.value,
           assignment_id=assignment.id, message=f"Assignment {assignment.id} published to {len(students)} students.",
           now=now, target_count=len(students))
    _audit(session, event_type="ASSIGNMENT_MISSION_CREATED", actor_account_id=actor.account_id,
           actor_role=actor.role.value, assignment_id=assignment.id, now=now, mission_id=mission.id,
           message=f"Assignment Guardian mission {mission.id} created for assignment {assignment.id}.")
    session.commit()
    return _publish_result(session, assignment, created=True)


def _publish_result(session: Session, assignment: Assignment, *, created: bool) -> PublishResult:
    count = session.execute(select(func.count(AssignmentTarget.id)).where(
        AssignmentTarget.assignment_id == assignment.id)).scalar_one()
    return PublishResult(assignment=view(session, assignment), created=created, target_count=count,
                         guardian_mission_id=assignment.guardian_mission_id)


def cancel(session: Session, actor: Any, assignment_id: int, now: datetime) -> Assignment:
    assignment = manageable(session, actor, assignment_id)
    if assignment.status == AssignmentStatus.CANCELLED:
        return assignment
    if assignment.status not in (AssignmentStatus.DRAFT, AssignmentStatus.PUBLISHED):
        raise AssignmentError("ASSIGNMENT_NOT_CANCELLABLE", f"Assignment is {assignment.status.value}.", 409)
    assignment.status, assignment.cancelled_at, assignment.updated_at = AssignmentStatus.CANCELLED, now, now
    closed = close_active_followups(session, assignment.id, now=now)
    if assignment.guardian_mission_id is not None:
        _events.publish(session, event_type=DomainEventType.ASSIGNMENT_CANCELLED, actor_account_id=actor.account_id,
                        subject_type=SUBJECT_MISSION, subject_id=assignment.guardian_mission_id, now=now,
                        payload={"assignment_id": assignment.id})
        wake_mission(session, assignment.guardian_mission_id, now)
    _audit(session, event_type="ASSIGNMENT_CANCELLED", actor_account_id=actor.account_id, actor_role=actor.role.value,
           assignment_id=assignment.id, message=f"Assignment {assignment.id} cancelled.", now=now,
           followups_closed=closed)
    session.commit()
    return assignment


# --- Student operations --------------------------------------------------------------------------------------------


def my_assignments(session: Session, actor: Any) -> List[MyAssignmentView]:
    student = own_student(session, actor)
    rows = session.execute(
        select(Assignment, Course.code, AssignmentSubmission.status, AssignmentSubmission.submitted_at)
        .join(AssignmentTarget, AssignmentTarget.assignment_id == Assignment.id)
        .join(Course, Course.id == Assignment.course_id)
        .outerjoin(AssignmentSubmission, (AssignmentSubmission.assignment_id == Assignment.id)
                   & (AssignmentSubmission.student_id == student.id))
        .where(AssignmentTarget.student_id == student.id, Assignment.status != AssignmentStatus.DRAFT)
        .order_by(Assignment.deadline_at, Assignment.id)
    ).all()
    return [MyAssignmentView(id=a.id, title=a.title, description=a.description, course_code=code, status=a.status,
                             deadline_at=a.deadline_at, submission_status=status, submitted_at=submitted_at)
            for a, code, status, submitted_at in rows]


def submit(session: Session, actor: Any, assignment_id: int, body: SubmitAssignment, now: datetime) -> AssignmentSubmission:
    student = own_student(session, actor)
    assignment = session.get(Assignment, assignment_id)
    if assignment is None or _target(session, assignment_id, student.id) is None:
        raise _not_found()  # not targeted: indistinguishable from missing
    if assignment.status != AssignmentStatus.PUBLISHED:
        raise AssignmentError("ASSIGNMENT_NOT_OPEN", f"Assignment is {assignment.status.value}.", 409)
    if session.execute(select(AssignmentSubmission.id).where(AssignmentSubmission.assignment_id == assignment_id,
                                                             AssignmentSubmission.student_id == student.id)).first():
        raise AssignmentError("ALREADY_SUBMITTED", "You have already submitted this assignment.", 409)
    status = rules.classify_submission(now, assignment.deadline_at)
    submission = AssignmentSubmission(assignment_id=assignment_id, student_id=student.id, status=status,
                                      submitted_by_account_id=actor.account_id, submitted_at=now,
                                      content_text=body.content_text, created_at=now)
    session.add(submission)
    try:
        session.flush()
    except IntegrityError:  # a concurrent duplicate submission won the unique (assignment, student) index
        session.rollback()
        raise AssignmentError("ALREADY_SUBMITTED", "You have already submitted this assignment.", 409) from None
    close_active_followups(session, assignment_id, student_id=student.id, now=now)
    payload = {"assignment_id": assignment_id, "student_id": student.id, "status": status.value}
    if assignment.guardian_mission_id is not None:
        _events.publish(session, event_type=DomainEventType.ASSIGNMENT_SUBMITTED, actor_account_id=actor.account_id,
                        subject_type=SUBJECT_MISSION, subject_id=assignment.guardian_mission_id, now=now, payload=payload)
        wake_mission(session, assignment.guardian_mission_id, now)
    else:
        _events.publish(session, event_type=DomainEventType.ASSIGNMENT_SUBMITTED, actor_account_id=actor.account_id,
                        subject_type=SUBJECT, subject_id=assignment_id, now=now, payload=payload)
    _audit(session, event_type="ASSIGNMENT_SUBMITTED", actor_account_id=actor.account_id, actor_role=actor.role.value,
           assignment_id=assignment_id, message=f"Assignment {assignment_id}: a submission was recorded ({status.value}).",
           now=now, student_id=student.id, status=status.value)
    session.commit()
    return submission


# --- Guardian support ----------------------------------------------------------------------------------------------


wake_mission = events_wake_mission  # shared with the Exam and Attendance Guardians (``app.agentos.events``)


def close_active_followups(session: Session, assignment_id: int, *, now: datetime, student_id: Optional[int] = None) -> int:
    """Close active follow-up requests (no reminder after a submission, a cancellation or the deadline)."""
    query = select(AssignmentFollowup).where(AssignmentFollowup.assignment_id == assignment_id,
                                             AssignmentFollowup.status == FollowupStatus.REQUESTED)
    if student_id is not None:
        query = query.where(AssignmentFollowup.student_id == student_id)
    rows = list(session.execute(query).scalars())
    for row in rows:
        row.status, row.updated_at = FollowupStatus.CANCELLED, now
    return len(rows)


Purpose = Literal["submission_reminder", "deadline_warning"]
Channel = Literal["in_app", "sms", "voice_call"]


def request_followups(
    session: Session, *, mission: AgentMission, assignment: Assignment, student_ids: Iterable[int], purpose: str,
    preferred_channel: Optional[str], policy: rules.FollowupPolicy, now: datetime, actor_account_id: int,
) -> List[Dict[str, Any]]:
    """Decide each requested follow-up in code. Allowed ones become an AssignmentFollowup plus a
    COMMUNICATION_REQUESTED event (ids, purpose, urgency, channel preference -- no contact details)."""
    ids = list(dict.fromkeys(int(s) for s in student_ids))[:MAX_FOLLOWUPS_PER_REQUEST]
    targets = set(session.execute(select(AssignmentTarget.student_id).where(
        AssignmentTarget.assignment_id == assignment.id, AssignmentTarget.student_id.in_(ids or [-1]))).scalars())
    submitted = set(session.execute(select(AssignmentSubmission.student_id).where(
        AssignmentSubmission.assignment_id == assignment.id, AssignmentSubmission.student_id.in_(ids or [-1]))).scalars())
    states = followup_states(session, assignment.id, ids)
    results: List[Dict[str, Any]] = []
    for sid in ids:
        state = states[sid]
        decision = rules.evaluate_followup(
            now=now, assignment_status=assignment.status, published_at=assignment.published_at or now,
            deadline_at=assignment.deadline_at, is_target=sid in targets, has_submitted=sid in submitted,
            active_followup=state.active, attempts_so_far=state.attempts, last_requested_at=state.last_requested_at,
            policy=policy)
        if not decision.allowed:
            _audit(session, event_type="ASSIGNMENT_FOLLOWUP_SUPPRESSED", actor_account_id=actor_account_id,
                   actor_role=AGENT_ROLE, assignment_id=assignment.id, now=now, mission_id=mission.id,
                   student_id=sid, reason=decision.reason,
                   message=f"Assignment {assignment.id}: follow-up suppressed ({decision.reason}).")
            results.append({"student_id": sid, "result": "suppressed", "reason": decision.reason})
            continue
        followup = AssignmentFollowup(
            assignment_id=assignment.id, student_id=sid, mission_id=mission.id, channel_requested=preferred_channel,
            purpose=purpose, urgency=decision.urgency, status=FollowupStatus.REQUESTED,
            attempt_number=decision.attempt_number, reason=decision.reason, not_before_at=decision.not_before,
            created_at=now, updated_at=now)
        session.add(followup)
        session.flush()
        _events.publish(session, event_type=DomainEventType.COMMUNICATION_REQUESTED, actor_account_id=actor_account_id,
                        subject_type="assignment_followup", subject_id=followup.id, now=now, payload={
                            "mission_id": mission.id, "assignment_id": assignment.id, "student_id": sid,
                            "purpose": purpose, "urgency": decision.urgency, "preferred_channel": preferred_channel,
                            "followup_id": followup.id,
                            "not_before": decision.not_before.isoformat() if decision.not_before else None})
        _audit(session, event_type="ASSIGNMENT_FOLLOWUP_REQUESTED", actor_account_id=actor_account_id,
               actor_role=AGENT_ROLE, assignment_id=assignment.id, now=now, mission_id=mission.id, student_id=sid,
               followup_id=followup.id, attempt_number=decision.attempt_number, urgency=decision.urgency,
               message=f"Assignment {assignment.id}: follow-up {followup.id} requested.")
        results.append({"student_id": sid, "result": "requested", "attempt_number": decision.attempt_number,
                        "urgency": decision.urgency, "deferred_for_quiet_hours": decision.not_before is not None})
    return results
