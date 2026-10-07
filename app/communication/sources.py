"""Source adapters: the Guardian follow-up behind a communication job, always reloaded from the database.

One adapter per source type (assignment / exam / attendance). Each loads the follow-up in the caller's tenant
session (another organization's row is filtered out and behaves exactly as missing), says whether the issue it
asks about still needs contact -- through the owning domain's own rule (``open_followup_refusal`` in
``assignment_rules`` / ``exam_rules`` / ``attendance_intervention``), from recorded facts only -- closes the
follow-up when its job ends, and builds the short, safe facts a message may mention (a title, a course code, a time).

Failure is controlled: a missing follow-up or a missing linked record (assignment, exam, intervention, class
session, course) is reported as ``SourceUnavailable`` with a code, never an ``AttributeError``. No adapter returns
a name, phone number, e-mail address, credential or any other student-profile field.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.academic import Course, Exam
from app.db.models.assignment import Assignment, AssignmentFollowup, AssignmentSubmission, FollowupStatus
from app.db.models.attendance_intervention import AttendanceFollowup, AttendanceIntervention, InterventionStatus
from app.db.models.exam import ExamAttendance, ExamFollowup
from app.db.models.faculty import AttendanceSession
from app.rules import assignment_rules, attendance_intervention as attendance_rules, exam_rules
from app.services import attendance_monitor
from app.services.class_schedule import local

FOLLOWUP_SUBJECTS = {"assignment_followup": "assignment", "exam_followup": "exam",
                     "attendance_followup": "attendance"}
MAX_TITLE = 120

# Controlled error codes (``SourceUnavailable.code``).
FOLLOWUP_NOT_FOUND = "FOLLOWUP_NOT_FOUND"
SOURCE_RECORD_MISSING = "SOURCE_RECORD_MISSING"
FOLLOWUP_CLOSED = "FOLLOWUP_CLOSED"


class SourceUnavailable(Exception):
    """The follow-up or a record it links to is missing (or belongs to another organization). ``code`` is safe to
    persist; the message is not shown anywhere."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SourceFacts:
    """What a message about this source may say. Safe to show the recipient; never contact or profile details."""

    kind: str  # assignment | exam | class
    title: str
    course_code: Optional[str]
    when: Optional[datetime]  # the deadline / exam start / class start

    def when_text(self) -> str:
        return local(self.when).strftime("%d %b %Y, %I:%M %p IST") if self.when is not None else "soon"


def _require(record: Any) -> Any:
    if record is None:
        raise SourceUnavailable(SOURCE_RECORD_MISSING)
    return record


def _course_code(session: Session, course_id: Optional[int]) -> str:
    return _require(session.get(Course, course_id) if course_id is not None else None).code


class SourceAdapter:
    source_type: str
    model: Any

    def load(self, session: Session, followup_id: int) -> Any:
        """The follow-up in this tenant session. Missing (or another organization's) -> ``SourceUnavailable``."""
        followup = session.get(self.model, followup_id) if isinstance(followup_id, int) else None
        if followup is None:
            raise SourceUnavailable(FOLLOWUP_NOT_FOUND)
        return followup

    def source_id(self, followup: Any) -> int:
        raise NotImplementedError

    def closed_reason(self, session: Session, followup: Any, now: datetime) -> Optional[str]:
        """None while contact is still needed; else a code (the follow-up is closed, or the domain rule's refusal).
        Raises ``SourceUnavailable`` when a linked record is missing."""
        raise NotImplementedError

    def is_open(self, session: Session, followup: Any, now: datetime) -> bool:
        try:
            return self.closed_reason(session, followup, now) is None
        except SourceUnavailable:
            return False

    def facts(self, session: Session, followup: Any) -> SourceFacts:
        raise NotImplementedError

    @staticmethod
    def close(followup: Any, status: FollowupStatus, *, job_id: Optional[int], outcome_code: Optional[str],
              now: datetime) -> bool:
        """Close an active follow-up once (a follow-up already closed by its domain keeps that status)."""
        if job_id is not None:
            followup.communication_job_id = job_id
        if followup.status != FollowupStatus.REQUESTED:
            return False
        followup.status, followup.outcome_code, followup.updated_at = status, (outcome_code or "")[:32] or None, now
        if status == FollowupStatus.DELIVERED:
            followup.delivered_at = now
        return True


class AssignmentSource(SourceAdapter):
    source_type, model = "assignment", AssignmentFollowup

    def source_id(self, followup: AssignmentFollowup) -> int:
        return followup.assignment_id

    def closed_reason(self, session: Session, followup: AssignmentFollowup, now: datetime) -> Optional[str]:
        if followup.status != FollowupStatus.REQUESTED:
            return FOLLOWUP_CLOSED
        assignment = _require(session.get(Assignment, followup.assignment_id))
        submitted = session.execute(select(AssignmentSubmission.id).where(
            AssignmentSubmission.assignment_id == assignment.id,
            AssignmentSubmission.student_id == followup.student_id)).first() is not None
        refusal = assignment_rules.open_followup_refusal(now=now, assignment_status=assignment.status,
                                                         deadline_at=assignment.deadline_at, has_submitted=submitted)
        return refusal.value if refusal is not None else None

    def facts(self, session: Session, followup: AssignmentFollowup) -> SourceFacts:
        assignment = _require(session.get(Assignment, followup.assignment_id))
        return SourceFacts("assignment", (assignment.title or "your assignment")[:MAX_TITLE],
                           _course_code(session, assignment.course_id), assignment.deadline_at)


class ExamSource(SourceAdapter):
    source_type, model = "exam", ExamFollowup

    def __init__(self, policy: Optional[exam_rules.ExamPolicy] = None) -> None:
        self.policy = policy or exam_rules.ExamPolicy()

    def source_id(self, followup: ExamFollowup) -> int:
        return followup.exam_id

    def closed_reason(self, session: Session, followup: ExamFollowup, now: datetime) -> Optional[str]:
        if followup.status != FollowupStatus.REQUESTED:
            return FOLLOWUP_CLOSED
        exam = _require(session.get(Exam, followup.exam_id))
        attendance = session.execute(select(ExamAttendance.status).where(
            ExamAttendance.exam_id == exam.id, ExamAttendance.student_id == followup.student_id)).scalar()
        terminal = exam_rules.terminal_deadline(exam.scheduled_end, exam.makeup_deadline_at, self.policy)
        refusal = exam_rules.open_followup_refusal(now=now, purpose=followup.purpose, exam_status=exam.status,
                                                   start=exam.scheduled_start, terminal=terminal, attendance=attendance)
        return refusal.value if refusal is not None else None

    def facts(self, session: Session, followup: ExamFollowup) -> SourceFacts:
        exam = _require(session.get(Exam, followup.exam_id))
        return SourceFacts("exam", (exam.title or "your exam")[:MAX_TITLE], _course_code(session, exam.course_id),
                           exam.scheduled_start)


class AttendanceSource(SourceAdapter):
    source_type, model = "attendance", AttendanceFollowup

    def __init__(self, policy: Optional[attendance_rules.AttendancePolicy] = None) -> None:
        self.policy = policy or attendance_rules.AttendancePolicy()

    def source_id(self, followup: AttendanceFollowup) -> int:
        return followup.intervention_id

    def closed_reason(self, session: Session, followup: AttendanceFollowup, now: datetime) -> Optional[str]:
        if followup.status != FollowupStatus.REQUESTED:
            return FOLLOWUP_CLOSED
        intervention = _require(session.get(AttendanceIntervention, followup.intervention_id))
        _require(session.get(AttendanceSession, intervention.attendance_session_id))
        facts = attendance_monitor.case_facts(session, intervention, self.policy)  # the Guardian's own facts
        refusal = attendance_rules.open_followup_refusal(
            now=now, intervention_open=intervention.status == InterventionStatus.OPEN, resolved_by=facts.resolved_by,
            deadline=facts.deadline)
        return refusal.value if refusal is not None else None

    def facts(self, session: Session, followup: AttendanceFollowup) -> SourceFacts:
        intervention = _require(session.get(AttendanceIntervention, followup.intervention_id))
        row = _require(session.get(AttendanceSession, intervention.attendance_session_id))
        code = _course_code(session, intervention.course_id)
        return SourceFacts("class", f"{code} class", code, row.scheduled_start)


def build_adapters(exam_policy: Optional[exam_rules.ExamPolicy] = None,
                   attendance_policy: Optional[attendance_rules.AttendancePolicy] = None) -> Dict[str, SourceAdapter]:
    """One adapter per source type. Pass the same policies the Guardians run under (``*_guardian.policy_from_env``)."""
    return {a.source_type: a for a in (AssignmentSource(), ExamSource(exam_policy), AttendanceSource(attendance_policy))}


def adapter_for(adapters: Dict[str, SourceAdapter], source_type: str) -> SourceAdapter:
    adapter = adapters.get(source_type)
    if adapter is None:
        raise SourceUnavailable(FOLLOWUP_NOT_FOUND)
    return adapter
