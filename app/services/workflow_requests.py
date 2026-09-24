"""Student workflow requests (Phase 16): context, routing, drafts, decisions.

Deterministic throughout. The Permission Agent hands this module a
structured ``PermissionIntent``; everything after that is plain code:

* the event is resolved against real events (``resolve_event``), never taken
  on the LLM's word;
* the request date is computed from the campus clock;
* affected classes, their faculty and the student's attendance come from the
  database and ``compute_attendance``;
* the reviewer comes from ``app/rules/request_routing.py``.

A prepared request is persisted as a DRAFT belonging to the student. It is
sent (PENDING) only by the student's explicit confirmation, and decided only
by the faculty member it was routed to.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.auth import AuthAccount
from app.db.models.communication import Notification, NotificationStatus
from app.db.models.events import Event, EventStatus
from app.db.models.faculty import FacultyProfile
from app.db.models.identity import Student
from app.db.models.workflow import WorkflowRequest, WorkflowRequestStatus, WorkflowRequestType
from app.db.repositories import operations_audit
from app.db.repositories.students import get_student_by_id
from app.rules.attendance import compute_attendance
from app.rules.attendance_standing import attendance_standing
from app.rules.class_session import overlaps
from app.rules.request_routing import Reviewer, RoutingDecision, route_request
from app.schemas.workflow import (
    REQUEST_TYPE_LABELS,
    AffectedClass,
    PermissionIntent,
    RequestContext,
    RequestEvent,
    WorkflowRequestView,
)
from app.services.class_schedule import (
    CAMPUS_TZ,
    PlannedClass,
    clock,
    get_student_attendance_for_session,
    hhmm,
    local,
    planned_classes,
    student_assignments,
)
from app.services.faculty_ops import course_counters, required_percentage
from app.services.knowledge import KnowledgeService

# Deterministic day parts (campus time): morning is before 13:00.
AFTERNOON_STARTS = time(13, 0)
_STOPWORDS = {"the", "a", "an", "to", "for", "of", "and", "in", "on", "at", "my", "i", "me", "need", "permission",
              "attend", "attending", "event", "participate", "join", "go", "want", "please", "request"}


class RequestError(Exception):
    """The request cannot be found, belongs to someone else, or cannot change now."""

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Event resolution
# ---------------------------------------------------------------------------


def _tokens(text: str) -> set:
    words = {w.rstrip("s") if len(w) > 3 else w for w in re.findall(r"[a-z0-9]+", text.lower())}
    return {w for w in words if w not in _STOPWORDS}


def resolve_event(message: str, reference: Optional[str], events: Sequence[Event]) -> List[Event]:
    """Events the student's words name. One result = resolved; several = ask; none = not found.

    A reference proposed by the LLM counts only if it literally appears in the
    message. An event matches when every meaningful word of the reference is
    in its title, or when its full title appears in the message.
    """
    text = message.lower()
    named = [e for e in events if e.title.lower() in text]
    if named:
        return named
    ref = reference if reference and reference.lower() in text else None
    wanted = _tokens(ref) if ref else set()
    matches = [e for e in events if wanted and wanted <= _tokens(e.title)]
    if not matches and not ref:
        message_tokens = _tokens(message)
        scored = [(len(_tokens(e.title) & message_tokens), e) for e in events]
        best = max((s for s, _ in scored), default=0)
        if best >= 2:
            matches = [e for s, e in scored if s == best]
    return matches


def open_events(session: Session, now: datetime) -> List[Event]:
    return list(
        session.execute(
            select(Event).where(
                Event.end_at > now, Event.status.notin_([EventStatus.CANCELLED, EventStatus.COMPLETED])
            ).order_by(Event.start_at)
        ).scalars().all()
    )


# ---------------------------------------------------------------------------
# Context collection
# ---------------------------------------------------------------------------


@dataclass
class Collected:
    request_type: WorkflowRequestType
    title: str
    context: RequestContext
    affected: List[PlannedClass] = field(default_factory=list)


@dataclass
class NeedsInput:
    message: str
    options: List[str] = field(default_factory=list)


def _resolve_date(intent: PermissionIntent, now: datetime) -> Optional[date]:
    today = local(now).date()
    return {
        "today": today, "tomorrow": today + timedelta(days=1), "yesterday": today - timedelta(days=1),
        "specific_date": intent.specific_date,
    }.get(intent.date_reference)


def _day_window(day: date, part: str) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0, 0), tzinfo=CAMPUS_TZ)
    end = start + timedelta(days=1)
    split = datetime.combine(day, AFTERNOON_STARTS, tzinfo=CAMPUS_TZ)
    if part == "morning":
        return start, split
    if part == "afternoon":
        return split, end
    return start, end


def _affected(session: Session, student: Student, start: datetime, end: datetime) -> List[PlannedClass]:
    assignments = student_assignments(session, student)
    found: List[PlannedClass] = []
    day = local(start).date()
    while day <= local(end - timedelta(seconds=1)).date():
        found += [
            p for p in planned_classes(session, assignments, day)
            if p.status != "cancelled" and overlaps(p.start, p.end, start, end)
        ]
        day += timedelta(days=1)
    return found


def _affected_view(
    session: Session, student: Student, item: PlannedClass, required: Optional[float]
) -> AffectedClass:
    counters = course_counters(session, item.assignment, student)
    calc = None
    if counters is not None and required is not None:
        calc = compute_attendance(counters.classes_attended, counters.classes_conducted, Decimal(str(required)))
    mark = get_student_attendance_for_session(session, item.session.id, student.id) if item.session else None
    return AffectedClass(
        course_code=item.assignment.course.code, course_title=item.assignment.course.title,
        section=item.assignment.section, starts_at=item.start, ends_at=item.end, start_local=hhmm(item.start),
        end_local=hhmm(item.end), room=item.room, faculty_name=item.assignment.faculty.full_name,
        session_status=item.status, my_mark=mark.status.value if mark else None,
        attendance_percentage=float(calc.current_percentage) if calc and calc.current_percentage is not None else None,
        required_percentage=required, standing=attendance_standing(calc).value,
    )


def collect_context(
    session: Session, student: Student, intent: PermissionIntent, message: str, now: datetime, required: Optional[float],
) -> Collected | NeedsInput:
    """Everything the reviewer needs, gathered deterministically from the database."""
    if intent.request_type == "unclear":
        return NeedsInput(
            "I can prepare event permission, attendance permission, leave and on-duty (OD) requests. "
            "Tell me which one you need, for which day or event, and why."
        )
    request_type = WorkflowRequestType(intent.request_type)
    label = REQUEST_TYPE_LABELS[request_type.value]
    base = RequestContext(
        student_name=student.user.full_name, student_year=student.year, student_section=student.section,
        department_code=student.department.code,
    )

    event: Optional[Event] = None
    if request_type == WorkflowRequestType.EVENT_PERMISSION or (
        request_type == WorkflowRequestType.OD_REQUEST and intent.event_reference
    ):
        candidates = resolve_event(message, intent.event_reference, open_events(session, now))
        if not candidates:
            return NeedsInput("I couldn't find an upcoming campus event matching that. Which event is it? Use its name as listed in Events.")
        if len(candidates) > 1:
            return NeedsInput("More than one event matches. Which one do you mean?", options=[e.title for e in candidates])
        event = candidates[0]

    if event is not None:
        start, end = event.start_at, event.end_at
        base.event = RequestEvent(event_id=event.id, title=event.title, starts_at=start, ends_at=end, location=event.location)
        base.request_date = local(start).date()
        title = f"{label}: {event.title}"
    else:
        day = _resolve_date(intent, now)
        if day is None:
            return NeedsInput(f"Which day is this {label.lower()} for? You can say today, tomorrow, yesterday or a date.")
        today = local(now).date()
        if request_type == WorkflowRequestType.ATTENDANCE_PERMISSION and day > today:
            return NeedsInput("Attendance permission is for a class you have already missed. For a future day, ask for leave instead.")
        if request_type in (WorkflowRequestType.LEAVE_REQUEST, WorkflowRequestType.OD_REQUEST) and day < today:
            return NeedsInput(f"A {label.lower()} must be for today or a later day. For a class you already missed, ask for attendance permission.")
        part = "full_day" if request_type == WorkflowRequestType.ATTENDANCE_PERMISSION else intent.day_part
        start, end = _day_window(day, part)
        base.request_date, base.day_part = day, part
        suffix = "" if part == "full_day" else f" ({part})"
        title = f"{label} for {day.strftime('%a %d %b %Y')}{suffix}"

    affected = _affected(session, student, start, end)
    base.window_start, base.window_end = start, end
    base.affected_classes = [_affected_view(session, student, p, required) for p in affected]
    base.timetable_conflict = bool(affected)
    if event is not None and not affected:
        base.notes.append(f"{event.title} ({clock(start)}–{clock(end)}) does not overlap any of your classes.")
    if request_type == WorkflowRequestType.ATTENDANCE_PERMISSION and not affected:
        return NeedsInput(f"You had no classes on {base.request_date.strftime('%a %d %b')}, so there is no attendance to excuse.")
    return Collected(request_type=request_type, title=title, context=base, affected=affected)


def route(session: Session, student: Student, collected: Collected) -> RoutingDecision:
    affected = [Reviewer(p.assignment.faculty.id, p.assignment.faculty.full_name) for p in collected.affected]
    mentor = session.get(FacultyProfile, student.mentor_faculty_id) if student.mentor_faculty_id else None
    return route_request(
        collected.request_type.value, affected, Reviewer(mentor.id, mentor.full_name) if mentor else None
    )


# ---------------------------------------------------------------------------
# Persistence and lifecycle
# ---------------------------------------------------------------------------


def _code() -> str:
    return f"REQ-{uuid.uuid4().hex[:8].upper()}"


def create_draft(
    session: Session, account: AuthAccount, student: Student, collected: Collected, routing: RoutingDecision,
    reason: str, now: datetime,
) -> WorkflowRequest:
    request = WorkflowRequest(
        request_code=_code(), request_type=collected.request_type, status=WorkflowRequestStatus.DRAFT,
        requester_account_id=account.id, requester_role=account.role.value, student_id=student.student_code,
        reviewer_faculty_id=routing.reviewer.faculty_id if routing.reviewer else None,
        department_id=student.department_id, title=collected.title, reason=reason,
        context=collected.context.model_dump(mode="json"), routing_basis=routing.basis, routing_note=routing.note,
        created_at=now,
    )
    session.add(request)
    session.flush()
    operations_audit.record(
        session, event_type="request_prepared", actor_account_id=account.id, actor_role=account.role.value,
        subject_type="workflow_request", subject_id=request.request_code,
        message=f"Draft prepared: {request.title}.", metadata={"routing_basis": routing.basis}, at=now,
    )
    session.commit()
    return request


def get_request(session: Session, request_code: str) -> Optional[WorkflowRequest]:
    return session.execute(select(WorkflowRequest).where(WorkflowRequest.request_code == request_code)).scalar_one_or_none()


def _own(session: Session, request_code: str, student_id: str) -> WorkflowRequest:
    request = get_request(session, request_code)
    if request is None or request.student_id != student_id:
        raise RequestError("That request was not found.", status_code=404)
    return request


def _notify(session: Session, student_id: str, title: str, body: str, now: datetime) -> None:
    student = get_student_by_id(session, student_id)
    if student is not None:
        session.add(Notification(
            student_id=student.id, title=title, body=body, category="requests", status=NotificationStatus.SENT, sent_at=now,
        ))


def _subject(request: WorkflowRequest) -> str:
    label = REQUEST_TYPE_LABELS[request.request_type.value]
    event = (request.context or {}).get("event")
    return f"{label} request for {event['title']}" if event else f"{label} request ({request.title.split(' for ', 1)[-1]})"


def submit(session: Session, account: AuthAccount, student_id: str, request_code: str, reason: Optional[str], now: datetime) -> WorkflowRequest:
    """The student's explicit Confirm & Send: DRAFT -> PENDING (or NEEDS_REVIEW with no reviewer)."""
    request = _own(session, request_code, student_id)
    if request.status != WorkflowRequestStatus.DRAFT:
        raise RequestError(f"This request was already {request.status.value.replace('_', ' ')}.")
    if reason:
        request.reason = reason.strip()
    request.status = WorkflowRequestStatus.PENDING if request.reviewer_faculty_id else WorkflowRequestStatus.NEEDS_REVIEW
    request.submitted_at = now
    if request.reviewer is not None:
        _notify(session, student_id, "Request sent", f"Your {_subject(request)} was sent to {request.reviewer.full_name}.", now)
    else:
        _notify(session, student_id, "Request needs review", f"Your {_subject(request)} was submitted, but no reviewer could be determined. {request.routing_note}", now)
    operations_audit.record(
        session, event_type="request_submitted", actor_account_id=account.id, actor_role=account.role.value,
        subject_type="workflow_request", subject_id=request.request_code,
        message=f"Submitted to {request.reviewer.full_name if request.reviewer else 'no reviewer (needs review)'}.",
        metadata={"status": request.status.value}, at=now,
    )
    session.commit()
    return request


def cancel(session: Session, account: AuthAccount, student_id: str, request_code: str, now: datetime) -> WorkflowRequest:
    request = _own(session, request_code, student_id)
    if request.status not in (WorkflowRequestStatus.DRAFT, WorkflowRequestStatus.PENDING, WorkflowRequestStatus.NEEDS_REVIEW):
        raise RequestError(f"This request was already {request.status.value.replace('_', ' ')}.")
    request.status = WorkflowRequestStatus.CANCELLED
    operations_audit.record(
        session, event_type="request_cancelled", actor_account_id=account.id, actor_role=account.role.value,
        subject_type="workflow_request", subject_id=request.request_code, message="Cancelled by the student.", at=now,
    )
    session.commit()
    return request


def decide(
    session: Session, account: AuthAccount, faculty: FacultyProfile, request_code: str, approve: bool,
    comment: Optional[str], now: datetime,
) -> WorkflowRequest:
    """Only the routed reviewer may decide, and only a PENDING request, exactly once."""
    request = get_request(session, request_code)
    if request is None or request.submitted_at is None:  # drafts (and discarded drafts) were never sent
        raise RequestError("That request was not found.", status_code=404)
    if request.reviewer_faculty_id != faculty.id:
        raise RequestError("This request is not assigned to you.", status_code=403)
    if request.status != WorkflowRequestStatus.PENDING:
        raise RequestError(f"This request was already {request.status.value.replace('_', ' ')}.")
    request.status = WorkflowRequestStatus.APPROVED if approve else WorkflowRequestStatus.REJECTED
    request.decided_at = now
    request.decided_by_account_id = account.id
    request.decision_reason = comment.strip() if comment and comment.strip() else None
    verb = "approved" if approve else "rejected"
    body = f"Your {_subject(request)} was {verb} by {faculty.full_name}."
    if request.decision_reason:
        body += f" Comment: {request.decision_reason}"
    _notify(session, request.student_id, f"Request {verb}", body, now)
    operations_audit.record(
        session, event_type=f"request_{verb}", actor_account_id=account.id, actor_role=account.role.value,
        subject_type="workflow_request", subject_id=request.request_code, message=body,
        metadata={"comment": request.decision_reason}, at=now,
    )
    session.commit()
    return request


def list_for_student(session: Session, student_id: str) -> List[WorkflowRequest]:
    return list(
        session.execute(
            select(WorkflowRequest)
            .where(WorkflowRequest.student_id == student_id, WorkflowRequest.submitted_at.is_not(None))
            .order_by(WorkflowRequest.id.desc())
        ).scalars().all()
    )


def list_for_reviewer(session: Session, faculty: FacultyProfile) -> List[WorkflowRequest]:
    return list(
        session.execute(
            select(WorkflowRequest)
            .where(WorkflowRequest.reviewer_faculty_id == faculty.id, WorkflowRequest.submitted_at.is_not(None))
            .order_by(WorkflowRequest.id.desc())
        ).scalars().all()
    )


def view(session: Session, request: WorkflowRequest) -> WorkflowRequestView:
    student = get_student_by_id(session, request.student_id) if request.student_id else None
    decider = session.get(AuthAccount, request.decided_by_account_id) if request.decided_by_account_id else None
    return WorkflowRequestView(
        request_id=request.request_code, request_type=request.request_type.value,
        type_label=REQUEST_TYPE_LABELS[request.request_type.value], status=request.status.value, title=request.title,
        reason=request.reason, student_id=request.student_id, student_name=student.user.full_name if student else None,
        reviewer_name=request.reviewer.full_name if request.reviewer else None, routing_basis=request.routing_basis,
        routing_note=request.routing_note, context=RequestContext.model_validate(request.context or {}),
        created_at=request.created_at, submitted_at=request.submitted_at, decided_at=request.decided_at,
        decided_by=decider.display_name if decider else None, decision_reason=request.decision_reason,
    )


def student_record(session: Session, student_id: str) -> Optional[Student]:
    return get_student_by_id(session, student_id)


def threshold(knowledge: KnowledgeService, now: datetime) -> Optional[float]:
    return required_percentage(knowledge, now)
