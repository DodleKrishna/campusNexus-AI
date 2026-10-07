"""Attendance monitoring (AgentOS V2 Phase 4): normalized attendance events, absence detection, interventions.

Deterministic application service on top of the Phase 16 class sessions. It never writes attendance: marks are
written only by the faculty operations (``app.services.faculty_ops``), which call the hooks here, or -- later --
by a biometric/ERP connector publishing the same event types.

* Hooks publish normalized domain events (subject ``attendance_session``): CLASS_STARTED, ATTENDANCE_MARKED (first
  mark) and ATTENDANCE_UPDATED (changed mark). For a student with an open intervention, an attending mark also
  sends ATTENDANCE_CORRECTED (an EXCUSED mark ATTENDANCE_JUSTIFIED) to the Guardian mission and wakes it; an
  approved leave/OD/attendance permission covering the class sends LEAVE_APPROVED; a staff member's approved
  justification (``justify``) sends ATTENDANCE_JUSTIFIED.
* ``process_attendance_events`` (run by the worker script beside the due-mission worker) consumes those session
  events once the absence grace period has passed and runs ``detect_absences``: every check of
  ``app.rules.attendance_intervention.detect_absence`` must pass, then exactly one ``AttendanceIntervention`` and
  one Attendance Guardian mission (owned by the class's faculty account) are created, ABSENCE_DETECTED is
  emitted, and both are audited. A duplicate is refused by the check *and* by the partial unique index.

Audit (``operation_audit_events``, subject ``attendance_intervention``): ATTENDANCE_ABSENCE_DETECTED,
ATTENDANCE_MISSION_CREATED, ATTENDANCE_JUSTIFICATION_APPROVED, ATTENDANCE_FOLLOWUP_REQUESTED,
ATTENDANCE_FOLLOWUP_SUPPRESSED (the Guardian adds ATTENDANCE_RESOLVED / ATTENDANCE_UNRESOLVED). Ids, statuses,
counts and codes only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agentos.events import SUBJECT_MISSION, EventService, wake_mission
from app.agentos.schemas import CreateAgentMission, DomainEventType
from app.db.models.agent_kernel import AgentMission, DomainEvent
from app.db.models.assignment import FollowupStatus
from app.db.models.attendance_intervention import AttendanceFollowup, AttendanceIntervention, InterventionStatus
from app.db.models.academic import Course
from app.db.models.auth import AuthAccount
from app.db.models.faculty import AttendanceSession, SessionAttendanceMark, TeachingAssignment
from app.db.models.identity import Student
from app.db.models.organization import Organization, OrganizationMembership
from app.db.models.workflow import WorkflowRequest, WorkflowRequestStatus
from app.db.repositories import operations_audit
from app.rules import attendance_intervention as rules
from app.schemas.enums import MembershipStatus, OrganizationStatus, UserRole
from app.services.assignments import headed_department_id
from app.services.class_schedule import roster

GUARDIAN_AGENT_KEY = "attendance_guardian"
GUARDIAN_MAX_STEPS = 40
SUBJECT = "attendance_intervention"
SUBJECT_SESSION = "attendance_session"
AGENT_ROLE = "agent"
STAFF_ROLES = frozenset({UserRole.FACULTY, UserRole.HOD, UserRole.ADMIN})
SCAN_EVENT_TYPES = (DomainEventType.CLASS_STARTED.value, DomainEventType.ATTENDANCE_MARKED.value,
                    DomainEventType.ATTENDANCE_UPDATED.value)
MAX_SCAN_EVENTS = 500
_events = EventService()


class AttendanceMonitorError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


def _audit(session: Session, *, event_type: str, actor_account_id: Optional[int], actor_role: str, intervention_id: int,
           message: str, now: datetime, **metadata: Any) -> None:
    operations_audit.record(session, event_type=event_type, actor_account_id=actor_account_id, actor_role=actor_role,
                            subject_type=SUBJECT, subject_id=str(intervention_id), message=message, at=now,
                            metadata={"intervention_id": intervention_id, **metadata})


def _signal(session: Session, intervention: AttendanceIntervention, event_type: DomainEventType,
            actor_account_id: Optional[int], now: datetime, **payload: Any) -> None:
    if intervention.mission_id is None:
        return
    _events.publish(session, event_type=event_type, actor_account_id=actor_account_id, subject_type=SUBJECT_MISSION,
                    subject_id=intervention.mission_id, now=now,
                    payload={"intervention_id": intervention.id, "session_id": intervention.attendance_session_id,
                             "student_id": intervention.student_id, **payload})
    wake_mission(session, intervention.mission_id, now)


def open_intervention(session: Session, session_id: int, student_id: int) -> Optional[AttendanceIntervention]:
    return session.execute(select(AttendanceIntervention).where(
        AttendanceIntervention.attendance_session_id == session_id, AttendanceIntervention.student_id == student_id,
        AttendanceIntervention.status == InterventionStatus.OPEN)).scalars().first()


# --- Facts (deterministic; shared by detection, the Guardian and the API) ----------------------------------------


def mark_of(session: Session, session_id: int, student_id: int) -> Optional[str]:
    status = session.execute(select(SessionAttendanceMark.status).where(
        SessionAttendanceMark.session_id == session_id, SessionAttendanceMark.student_id == student_id)).scalar()
    return status.value if status is not None else None


def _parse(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def approved_leave(session: Session, student: Student, row: AttendanceSession) -> bool:
    """An APPROVED leave / OD / attendance-permission request of this student whose window overlaps the class."""
    requests = session.execute(select(WorkflowRequest.request_type, WorkflowRequest.context).where(
        WorkflowRequest.student_id == student.student_code,
        WorkflowRequest.status == WorkflowRequestStatus.APPROVED)).all()
    return any(kind.value in rules.LEAVE_REQUEST_TYPES and rules.leave_covers(
        _parse((context or {}).get("window_start")), _parse((context or {}).get("window_end")),
        row.scheduled_start, row.scheduled_end) for kind, context in requests)


@dataclass(frozen=True)
class CaseFacts:
    session_status: str
    mark: Optional[str]
    leave_approved: bool
    resolved_by: Optional[str]
    deadline: datetime


def case_facts(session: Session, intervention: AttendanceIntervention, policy: rules.AttendancePolicy) -> CaseFacts:
    row = session.get(AttendanceSession, intervention.attendance_session_id)
    student = session.get(Student, intervention.student_id)
    mark = mark_of(session, intervention.attendance_session_id, intervention.student_id)
    leave = row is not None and student is not None and approved_leave(session, student, row)
    resolved_by = rules.resolution(mark=mark, detected_mark=intervention.detected_mark,
                                   justification_code=intervention.justification_code, leave_approved=leave)
    return CaseFacts(session_status=row.status.value if row is not None else "missing", mark=mark, leave_approved=leave,
                     resolved_by=resolved_by, deadline=rules.resolution_deadline(intervention.detected_at, policy))


# --- Hooks (called by the faculty operations / workflow decisions, inside their transaction) ----------------------


def class_started(session: Session, row: AttendanceSession, actor_account_id: Optional[int], now: datetime) -> None:
    _events.publish(session, event_type=DomainEventType.CLASS_STARTED, actor_account_id=actor_account_id,
                    subject_type=SUBJECT_SESSION, subject_id=row.id, now=now,
                    payload={"session_id": row.id, "teaching_assignment_id": row.teaching_assignment_id,
                             "course_id": row.course_id, "started_at": now.isoformat()})


def marks_changed(session: Session, row: AttendanceSession, changes: Iterable[Tuple[int, Optional[str], str]],
                  actor_account_id: Optional[int], now: datetime) -> None:
    """``changes``: (student pk, previous mark or None, new mark)."""
    for student_id, previous, status in changes:
        kind = DomainEventType.ATTENDANCE_MARKED if previous is None else DomainEventType.ATTENDANCE_UPDATED
        _events.publish(session, event_type=kind, actor_account_id=actor_account_id, subject_type=SUBJECT_SESSION,
                        subject_id=row.id, now=now,
                        payload={"session_id": row.id, "student_id": student_id, "status": status, "previous": previous})
        intervention = open_intervention(session, row.id, student_id)
        if intervention is None:
            continue
        if status in rules.ATTENDING_MARKS:
            _signal(session, intervention, DomainEventType.ATTENDANCE_CORRECTED, actor_account_id, now, status=status)
        elif status == "excused":
            _signal(session, intervention, DomainEventType.ATTENDANCE_JUSTIFIED, actor_account_id, now, status=status)


def leave_approved(session: Session, request: WorkflowRequest, actor_account_id: Optional[int], now: datetime) -> int:
    """An approved student leave/OD/attendance permission: wake every open intervention it covers."""
    if request.status != WorkflowRequestStatus.APPROVED or request.request_type.value not in rules.LEAVE_REQUEST_TYPES \
            or not request.student_id:
        return 0
    student = session.execute(select(Student).where(Student.student_code == request.student_id)).scalars().first()
    if student is None:
        return 0
    window = (_parse((request.context or {}).get("window_start")), _parse((request.context or {}).get("window_end")))
    woken = 0
    for intervention, row in session.execute(
            select(AttendanceIntervention, AttendanceSession)
            .join(AttendanceSession, AttendanceSession.id == AttendanceIntervention.attendance_session_id)
            .where(AttendanceIntervention.student_id == student.id,
                   AttendanceIntervention.status == InterventionStatus.OPEN)).all():
        if rules.leave_covers(window[0], window[1], row.scheduled_start, row.scheduled_end):
            _signal(session, intervention, DomainEventType.LEAVE_APPROVED, actor_account_id, now,
                    request_type=request.request_type.value)
            woken += 1
    return woken


# --- Detection -------------------------------------------------------------------------------------------------------


def _owner_actor(session: Session, organization_id: int, row: AttendanceSession) -> Optional[Any]:
    """The class's faculty account (active membership), as the Guardian mission's owner. None: nobody to own it."""
    from app.agentos.runtime import MissionActor

    found = session.execute(
        select(OrganizationMembership, AuthAccount).join(AuthAccount, AuthAccount.id == OrganizationMembership.account_id)
        .where(OrganizationMembership.faculty_profile_id == row.faculty_id,
               OrganizationMembership.status == MembershipStatus.ACTIVE, AuthAccount.is_active.is_(True))
        .order_by(OrganizationMembership.id)).first()
    if found is None:
        return None
    membership, account = found
    return MissionActor(organization_id=organization_id, account_id=account.id, role=membership.role,
                        faculty_profile_id=membership.faculty_profile_id)


@dataclass
class DetectionResult:
    student_id: int
    code: str
    intervention_id: Optional[int] = None
    mission_id: Optional[int] = None


def detect_absences(session: Session, runtime: Any, organization_id: int, row: AttendanceSession, now: datetime,
                    policy: rules.AttendancePolicy) -> List[DetectionResult]:
    """Run the deterministic absence checks for every rostered student; create one intervention + mission per
    confirmed unexplained absence. Stages rows in the caller's transaction (the caller commits)."""
    students = roster(session, session.get(TeachingAssignment, row.teaching_assignment_id))
    marks = {sid: status.value for sid, status in session.execute(select(
        SessionAttendanceMark.student_id, SessionAttendanceMark.status).where(SessionAttendanceMark.session_id == row.id))}
    roll_taken = any(m in rules.ATTENDING_MARKS for m in marks.values())
    existing = set(session.execute(select(AttendanceIntervention.student_id).where(
        AttendanceIntervention.attendance_session_id == row.id)).scalars())
    owner = None
    results: List[DetectionResult] = []
    for student in students:
        mark = marks.get(student.id)
        code = rules.detect_absence(
            session_status=row.status.value, started_at=row.actual_started_at, now=now, mark=mark, roll_taken=roll_taken,
            leave_approved=mark not in rules.ATTENDING_MARKS and approved_leave(session, student, row),
            has_intervention=student.id in existing, policy=policy)
        if code != rules.DetectionCode.ABSENCE_CONFIRMED:
            results.append(DetectionResult(student.id, code.value))
            continue
        owner = owner or _owner_actor(session, organization_id, row)
        if owner is None:
            results.append(DetectionResult(student.id, "NO_ACTIVE_CLASS_OWNER"))
            continue
        results.append(_create_intervention(session, runtime, owner, row, student, mark, now))
    return results


def _create_intervention(session: Session, runtime: Any, owner: Any, row: AttendanceSession, student: Student,
                         mark: Optional[str], now: datetime) -> DetectionResult:
    intervention = AttendanceIntervention(
        student_id=student.id, attendance_session_id=row.id, teaching_assignment_id=row.teaching_assignment_id,
        course_id=row.course_id, status=InterventionStatus.OPEN, detected_mark=mark or "unmarked", detected_at=now,
        created_at=now, updated_at=now)
    session.add(intervention)
    session.flush()  # a concurrent duplicate fails here on the partial unique index (the scanner rolls back)
    mission = runtime.create_mission(session, owner, CreateAgentMission(
        agent_key=GUARDIAN_AGENT_KEY, max_steps=GUARDIAN_MAX_STEPS, context={"intervention_id": intervention.id},
        goal=(f"Follow up attendance intervention {intervention.id} (one student's unexplained absence) until it is "
              "resolved by recorded facts or its resolution window closes, following institution communication policy."),
        success_criteria=["The absence is resolved by a recorded fact: corrected attendance, an approved justification "
                          "or approved leave (verified from the database).",
                          "Follow-ups are requested only when the communication policy allows them."],
    ), internal=True, commit=False, next_wake_at=now)
    intervention.mission_id = mission.id
    _events.publish(session, event_type=DomainEventType.ABSENCE_DETECTED, actor_account_id=owner.account_id,
                    subject_type=SUBJECT, subject_id=intervention.id, now=now,
                    payload={"intervention_id": intervention.id, "session_id": row.id, "student_id": student.id,
                             "mission_id": mission.id, "detected_mark": intervention.detected_mark})
    _audit(session, event_type="ATTENDANCE_ABSENCE_DETECTED", actor_account_id=None, actor_role="system",
           intervention_id=intervention.id, now=now, session_id=row.id, student_id=student.id,
           detected_mark=intervention.detected_mark, message=f"Absence confirmed for intervention {intervention.id}.")
    _audit(session, event_type="ATTENDANCE_MISSION_CREATED", actor_account_id=owner.account_id, actor_role="system",
           intervention_id=intervention.id, now=now, mission_id=mission.id,
           message=f"Attendance Guardian mission {mission.id} created for intervention {intervention.id}.")
    return DetectionResult(student.id, rules.DetectionCode.ABSENCE_CONFIRMED.value, intervention.id, mission.id)


class ScanReport(BaseModel):
    events_consumed: int = 0
    sessions_checked: int = 0
    interventions_created: int = 0
    skipped_codes: Dict[str, int] = {}


def process_attendance_events(factory: Any, runtime: Any, policy: rules.AttendancePolicy, *,
                              now: Optional[datetime] = None, limit: int = MAX_SCAN_EVENTS) -> ScanReport:
    """Consume due class-session attendance events (older than the absence grace period) and run absence detection
    for their sessions. Bounded: at most ``limit`` events per call. Deterministic: no AI call."""
    from app.db.tenant_session import TenantSessionFactory

    if not 1 <= limit <= MAX_SCAN_EVENTS:
        raise ValueError(f"limit must be 1-{MAX_SCAN_EVENTS}")
    factory = TenantSessionFactory.from_sessionmaker(factory)
    now = now or runtime.clock()
    report = ScanReport()
    with factory() as unbound:  # organizations are global; no tenant data is read here
        organization_ids = list(unbound.execute(select(Organization.id).where(
            Organization.status == OrganizationStatus.ACTIVE).order_by(Organization.id)).scalars())
    for organization_id in organization_ids:
        remaining = limit - report.events_consumed
        if remaining <= 0:
            break
        with factory.open_tenant_session(organization_id) as session:
            due = list(session.execute(select(DomainEvent).where(
                DomainEvent.consumed_at.is_(None), DomainEvent.subject_type == SUBJECT_SESSION,
                DomainEvent.event_type.in_(SCAN_EVENT_TYPES), DomainEvent.created_at <= now - policy.grace,
            ).order_by(DomainEvent.id).limit(remaining)).scalars())
            by_session: Dict[str, List[DomainEvent]] = {}
            for event in due:
                by_session.setdefault(event.subject_id or "", []).append(event)
            for subject_id, events in by_session.items():
                _scan_session(session, runtime, organization_id, subject_id, events, now, policy, report)
    return report


def _scan_session(session: Session, runtime: Any, organization_id: int, subject_id: str, events: List[DomainEvent],
                  now: datetime, policy: rules.AttendancePolicy, report: ScanReport) -> None:
    """One session, one transaction: detect, consume its events, commit. A concurrent duplicate detection rolls
    back and leaves the events for the next run (where the existing intervention is seen)."""
    created, skipped = 0, {}
    try:
        row = session.get(AttendanceSession, int(subject_id)) if subject_id.isdigit() else None
        if row is not None:
            for result in detect_absences(session, runtime, organization_id, row, now, policy):
                if result.intervention_id is not None:
                    created += 1
                else:
                    skipped[result.code] = skipped.get(result.code, 0) + 1
        for event in events:
            event.consumed_at = now
        session.commit()
    except IntegrityError:
        session.rollback()
        report.skipped_codes["CONCURRENT_DETECTION"] = report.skipped_codes.get("CONCURRENT_DETECTION", 0) + 1
        return
    report.sessions_checked += 1 if row is not None else 0
    report.interventions_created += created
    report.events_consumed += len(events)
    for code, count in skipped.items():
        report.skipped_codes[code] = report.skipped_codes.get(code, 0) + count


# --- Staff API -------------------------------------------------------------------------------------------------------


class JustifyAbsence(BaseModel):
    """A staff member approves the student's justification. The approver comes from the token."""

    model_config = ConfigDict(extra="forbid")

    justification_code: Literal[rules.JUSTIFICATION_CODES]  # type: ignore[valid-type]


class InterventionView(BaseModel):
    id: int
    status: InterventionStatus
    student_code: Optional[str]
    course_code: Optional[str]
    attendance_session_id: int
    class_starts_at: Optional[datetime]
    detected_mark: str
    current_mark: Optional[str]
    resolution_code: Optional[str]
    justification_code: Optional[str]
    mission_id: Optional[int]
    detected_at: datetime
    resolved_at: Optional[datetime]


def manageable(session: Session, actor: Any, intervention_id: int) -> AttendanceIntervention:
    """Admin, the class's faculty, or the head of the class's department. Anyone else: 404."""
    intervention = session.get(AttendanceIntervention, intervention_id) if actor.role in STAFF_ROLES else None
    if intervention is None:
        raise AttendanceMonitorError("INTERVENTION_NOT_FOUND", "Intervention not found.", 404)
    if actor.role == UserRole.ADMIN:
        return intervention
    teaching = session.get(TeachingAssignment, intervention.teaching_assignment_id)
    if teaching is not None and actor.faculty_profile_id is not None and (
            teaching.faculty_id == actor.faculty_profile_id
            or (actor.role == UserRole.HOD
                and teaching.department_id == headed_department_id(session, actor.faculty_profile_id))):
        return intervention
    raise AttendanceMonitorError("INTERVENTION_NOT_FOUND", "Intervention not found.", 404)


def view(session: Session, intervention: AttendanceIntervention) -> InterventionView:
    student = session.get(Student, intervention.student_id)
    course = session.get(Course, intervention.course_id)
    row = session.get(AttendanceSession, intervention.attendance_session_id)
    return InterventionView(
        id=intervention.id, status=intervention.status, student_code=student.student_code if student else None,
        course_code=course.code if course else None, attendance_session_id=intervention.attendance_session_id,
        class_starts_at=row.scheduled_start if row else None, detected_mark=intervention.detected_mark,
        current_mark=mark_of(session, intervention.attendance_session_id, intervention.student_id),
        resolution_code=intervention.resolution_code, justification_code=intervention.justification_code,
        mission_id=intervention.mission_id, detected_at=intervention.detected_at, resolved_at=intervention.resolved_at)


def list_for_staff(session: Session, actor: Any, *, status: Optional[InterventionStatus] = None,
                   limit: int = 50) -> List[AttendanceIntervention]:
    """Structural scope: an admin sees the organization; faculty their own classes; an HOD also their department."""
    if actor.role not in STAFF_ROLES:
        raise AttendanceMonitorError("ROLE_NOT_ALLOWED", "Only staff can list attendance interventions.", 403)
    query = select(AttendanceIntervention).join(
        TeachingAssignment, TeachingAssignment.id == AttendanceIntervention.teaching_assignment_id)
    if actor.role != UserRole.ADMIN:
        if actor.faculty_profile_id is None:
            return []
        department = headed_department_id(session, actor.faculty_profile_id) if actor.role == UserRole.HOD else None
        scope = TeachingAssignment.faculty_id == actor.faculty_profile_id
        if department is not None:
            scope = scope | (TeachingAssignment.department_id == department)
        query = query.where(scope)
    if status is not None:
        query = query.where(AttendanceIntervention.status == status)
    return list(session.execute(query.order_by(AttendanceIntervention.id.desc()).limit(max(1, min(limit, 100)))).scalars())


def justify(session: Session, actor: Any, intervention_id: int, body: JustifyAbsence, now: datetime) -> AttendanceIntervention:
    intervention = manageable(session, actor, intervention_id)
    if intervention.status != InterventionStatus.OPEN:
        raise AttendanceMonitorError("INTERVENTION_NOT_OPEN", f"Intervention is {intervention.status.value}.", 409)
    if intervention.justification_code is None:
        intervention.justification_code = body.justification_code
        intervention.justified_by_account_id, intervention.justified_at = actor.account_id, now
        intervention.updated_at = now
        _signal(session, intervention, DomainEventType.ATTENDANCE_JUSTIFIED, actor.account_id, now,
                justification_code=body.justification_code)
        _audit(session, event_type="ATTENDANCE_JUSTIFICATION_APPROVED", actor_account_id=actor.account_id,
               actor_role=actor.role.value, intervention_id=intervention.id, now=now,
               justification_code=body.justification_code,
               message=f"Intervention {intervention.id}: justification approved.")
    session.commit()
    return intervention


# --- Guardian support ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FollowupState:
    attempts: int
    active: bool
    last_requested_at: Optional[datetime]


def followup_state(session: Session, intervention_id: int) -> FollowupState:
    rows = session.execute(select(AttendanceFollowup.status, AttendanceFollowup.created_at).where(
        AttendanceFollowup.intervention_id == intervention_id)).all()
    return FollowupState(len(rows), any(s == FollowupStatus.REQUESTED for s, _ in rows),
                         max((at for _, at in rows), default=None))


def close_active_followups(session: Session, intervention_id: int, now: datetime) -> int:
    rows = list(session.execute(select(AttendanceFollowup).where(
        AttendanceFollowup.intervention_id == intervention_id,
        AttendanceFollowup.status == FollowupStatus.REQUESTED)).scalars())
    for row in rows:
        row.status, row.updated_at = FollowupStatus.CANCELLED, now
    return len(rows)


def request_followup(session: Session, *, mission: AgentMission, intervention: AttendanceIntervention,
                     preferred_channel: Optional[str], policy: rules.AttendancePolicy, now: datetime,
                     actor_account_id: int) -> Dict[str, Any]:
    """Decide the follow-up in code. Allowed: an AttendanceFollowup + COMMUNICATION_REQUESTED (ids only)."""
    facts = case_facts(session, intervention, policy)
    state = followup_state(session, intervention.id)
    decision = rules.evaluate_attendance_followup(
        now=now, intervention_open=intervention.status == InterventionStatus.OPEN, resolved_by=facts.resolved_by,
        deadline=facts.deadline, active_followup=state.active, attempts_so_far=state.attempts,
        last_requested_at=state.last_requested_at, policy=policy)
    if not decision.allowed:
        _audit(session, event_type="ATTENDANCE_FOLLOWUP_SUPPRESSED", actor_account_id=actor_account_id,
               actor_role=AGENT_ROLE, intervention_id=intervention.id, now=now, mission_id=mission.id,
               student_id=intervention.student_id, reason=decision.reason,
               message=f"Intervention {intervention.id}: follow-up suppressed ({decision.reason}).")
        return {"result": "suppressed", "reason": decision.reason}
    followup = AttendanceFollowup(
        intervention_id=intervention.id, student_id=intervention.student_id, mission_id=mission.id,
        channel_requested=preferred_channel, purpose=rules.PURPOSE, urgency=decision.urgency,
        status=FollowupStatus.REQUESTED, attempt_number=decision.attempt_number, reason=decision.reason,
        not_before_at=decision.not_before, created_at=now, updated_at=now)
    session.add(followup)
    session.flush()
    _events.publish(session, event_type=DomainEventType.COMMUNICATION_REQUESTED, actor_account_id=actor_account_id,
                    subject_type="attendance_followup", subject_id=followup.id, now=now, payload={
                        "mission_id": mission.id, "intervention_id": intervention.id,
                        "session_id": intervention.attendance_session_id, "student_id": intervention.student_id,
                        "followup_id": followup.id, "purpose": rules.PURPOSE, "urgency": decision.urgency,
                        "preferred_channel": preferred_channel,
                        "not_before": decision.not_before.isoformat() if decision.not_before else None})
    _audit(session, event_type="ATTENDANCE_FOLLOWUP_REQUESTED", actor_account_id=actor_account_id,
           actor_role=AGENT_ROLE, intervention_id=intervention.id, now=now, mission_id=mission.id,
           student_id=intervention.student_id, followup_id=followup.id, attempt_number=decision.attempt_number,
           urgency=decision.urgency, message=f"Intervention {intervention.id}: follow-up {followup.id} requested.")
    return {"result": "requested", "attempt_number": decision.attempt_number, "urgency": decision.urgency,
            "deferred_for_quiet_hours": decision.not_before is not None}
