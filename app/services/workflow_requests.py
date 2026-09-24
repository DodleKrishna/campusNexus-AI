"""Workflow requests (Phase 16 students, Phase 17 faculty): context, routing, drafts, decisions.

Deterministic throughout. The Permission Agent hands this module a
structured ``PermissionIntent``; everything after that is plain code:

* the event is resolved against real events (``resolve_event``), never taken
  on the LLM's word;
* the request date is computed from the campus clock;
* affected classes, their faculty and the student's attendance come from the
  database and ``compute_attendance``;
* the reviewer comes from ``app/rules/request_routing.py``.

A prepared request is persisted as a DRAFT belonging to its requester (a
student, or -- Phase 17 -- a faculty member). It is sent (PENDING) only by the
requester's explicit confirmation, and decided only by the person it was
routed to: a class faculty member or mentor for students (the department HOD
when neither can be found), the department HOD for faculty. Nobody decides
their own request.
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
from app.db.models.identity import Department, Student
from app.db.models.workflow import WorkflowRequest, WorkflowRequestStatus, WorkflowRequestType
from app.db.repositories import operations_audit
from app.db.repositories.students import get_student_by_id
from app.rules.attendance import compute_attendance
from app.rules.attendance_standing import attendance_standing
from app.rules.class_session import overlaps
from app.rules.request_routing import (
    ADMINISTRATION,
    Reviewer,
    RoutingDecision,
    route_faculty_request,
    route_hod_request,
    route_request,
)
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
from app.schemas.enums import UserRole
from app.services import account_notifications, staff_notifications
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


@dataclass
class Requester:
    """Who is asking: a student (``student`` set) or a faculty member/HOD (``faculty`` set)."""

    account: AuthAccount
    student: Optional[Student] = None
    faculty: Optional[FacultyProfile] = None

    @property
    def kind(self) -> str:
        return "student" if self.student is not None else "faculty"

    @property
    def is_department_head(self) -> bool:
        """Phase 18: an HOD account that heads its department asks the administration."""
        return (
            self.faculty is not None and self.account is not None and self.account.role == UserRole.HOD
            and self.faculty.department is not None and self.faculty.department.hod_faculty_id == self.faculty.id
        )


# Phase 17: what a faculty member's interpreted request becomes.
_FACULTY_TYPE_MAP = {
    "faculty_leave": WorkflowRequestType.FACULTY_LEAVE,
    "leave_request": WorkflowRequestType.FACULTY_LEAVE,
    "class_substitution": WorkflowRequestType.CLASS_SUBSTITUTION,
    "od_request": WorkflowRequestType.OD_REQUEST,
    "department_permission": WorkflowRequestType.DEPARTMENT_PERMISSION,
    "event_permission": WorkflowRequestType.DEPARTMENT_PERMISSION,
}
# Phase 18: what a department head's interpreted request becomes (routed to the administration).
_HOD_TYPE_MAP = {
    "faculty_leave": WorkflowRequestType.HOD_LEAVE,
    "leave_request": WorkflowRequestType.HOD_LEAVE,
    "class_substitution": WorkflowRequestType.CLASS_SUBSTITUTION,
    "od_request": WorkflowRequestType.OD_REQUEST,
    "department_permission": WorkflowRequestType.DEPARTMENT_PERMISSION,
    "event_permission": WorkflowRequestType.DEPARTMENT_PERMISSION,
    "department_resource": WorkflowRequestType.DEPARTMENT_RESOURCE,
    "admin_escalation": WorkflowRequestType.ADMIN_ESCALATION,
}
# Requests about something rather than a day: no date is required.
_UNDATED = {WorkflowRequestType.DEPARTMENT_RESOURCE, WorkflowRequestType.ADMIN_ESCALATION}
_STUDENT_TYPES = {"event_permission", "attendance_permission", "leave_request", "od_request"}


def collect_context(
    session: Session, requester: Requester, intent: PermissionIntent, message: str, now: datetime,
    required: Optional[float],
) -> Collected | NeedsInput:
    """Everything the reviewer needs, gathered deterministically from the database."""
    if requester.kind == "faculty":
        return _collect_faculty(session, requester.faculty, intent, message, now, head=requester.is_department_head)
    return _collect_student(session, requester.student, intent, message, now, required)


def _collect_student(
    session: Session, student: Student, intent: PermissionIntent, message: str, now: datetime, required: Optional[float],
) -> Collected | NeedsInput:
    if intent.request_type == "unclear":
        return NeedsInput(
            "I can prepare event permission, attendance permission, leave and on-duty (OD) requests. "
            "Tell me which one you need, for which day or event, and why."
        )
    if intent.request_type not in _STUDENT_TYPES:
        return NeedsInput("That kind of request is for faculty. Students can ask for event permission, attendance permission, leave or OD.")
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


def faculty_affected(session: Session, faculty: FacultyProfile, start: datetime, end: datetime) -> List[PlannedClass]:
    """The faculty member's own class meetings that overlap [start, end) (not cancelled)."""
    from app.services.faculty_ops import assignments_of

    mine = assignments_of(session, faculty)
    found: List[PlannedClass] = []
    day = local(start).date()
    while day <= local(end - timedelta(seconds=1)).date():
        found += [
            p for p in planned_classes(session, mine, day)
            if p.status != "cancelled" and overlaps(p.start, p.end, start, end)
        ]
        day += timedelta(days=1)
    return found


def _faculty_affected_view(session: Session, item: PlannedClass) -> AffectedClass:
    from app.services.class_schedule import roster

    a = item.assignment
    return AffectedClass(
        course_code=a.course.code, course_title=a.course.title, section=a.section, starts_at=item.start, ends_at=item.end,
        start_local=hhmm(item.start), end_local=hhmm(item.end), room=item.room, faculty_name=a.faculty.full_name,
        session_status=item.status, class_label=f"{a.department.code} {a.year}-{a.section}",
        roster_size=len(roster(session, a)), substitute="Not assigned",
    )


def _collect_faculty(
    session: Session, faculty: FacultyProfile, intent: PermissionIntent, message: str, now: datetime, *, head: bool = False,
) -> Collected | NeedsInput:
    request_type = (_HOD_TYPE_MAP if head else _FACULTY_TYPE_MAP).get(intent.request_type)
    if request_type is None:
        if head:
            return NeedsInput(
                "I can prepare HOD leave, department resource, department permission, OD and escalation requests "
                "to the administration. Tell me which one you need, and why."
            )
        return NeedsInput(
            "I can prepare faculty leave, class substitution, on-duty (OD) and department permission requests. "
            "Tell me which one you need, for which day, and why."
        )
    label = REQUEST_TYPE_LABELS[request_type.value]
    base = RequestContext(
        requester_kind="faculty", faculty_name=faculty.full_name, faculty_designation=faculty.designation,
        faculty_employee_code=faculty.employee_code, department_code=faculty.department.code,
    )
    event: Optional[Event] = None
    if intent.event_reference:
        candidates = resolve_event(message, intent.event_reference, open_events(session, now))
        if len(candidates) > 1:
            return NeedsInput("More than one event matches. Which one do you mean?", options=[e.title for e in candidates])
        event = candidates[0] if candidates else None
    if event is not None:
        start, end = event.start_at, event.end_at
        base.event = RequestEvent(event_id=event.id, title=event.title, starts_at=start, ends_at=end, location=event.location)
        base.request_date = local(start).date()
        title = f"{label}: {event.title}"
    elif request_type in _UNDATED and _resolve_date(intent, now) is None:
        summary = (intent.reason or message).strip().rstrip(".")
        title = f"{label}: {summary[:80]}"
        base.notes.append("Not tied to a date, so no classes are affected.")
        return Collected(request_type=request_type, title=title, context=base, affected=[])
    else:
        day = _resolve_date(intent, now)
        if day is None:
            return NeedsInput(f"Which day is this {label.lower()} for? You can say today, tomorrow or a date.")
        if day < local(now).date():
            return NeedsInput(f"A {label.lower()} request must be for today or a later day.")
        start, end = _day_window(day, intent.day_part)
        base.request_date, base.day_part = day, intent.day_part
        suffix = "" if intent.day_part == "full_day" else f" ({intent.day_part})"
        title = f"{label} for {day.strftime('%a %d %b %Y')}{suffix}"
    affected = faculty_affected(session, faculty, start, end)
    base.window_start, base.window_end = start, end
    base.affected_classes = [_faculty_affected_view(session, p) for p in affected]
    base.timetable_conflict = bool(affected)
    if not affected:
        base.notes.append("None of your classes fall in this period.")
    elif request_type in (WorkflowRequestType.FACULTY_LEAVE, WorkflowRequestType.CLASS_SUBSTITUTION, WorkflowRequestType.OD_REQUEST):
        base.notes.append(
            "Substitutes are not assigned automatically; " + ("the administration arranges cover." if head else "the HOD arranges cover.")
        )
    if request_type == WorkflowRequestType.CLASS_SUBSTITUTION and not affected:
        return NeedsInput("You have no classes in that period, so there is nothing to substitute.")
    return Collected(request_type=request_type, title=title, context=base, affected=affected)


def route(session: Session, requester: Requester, collected: Collected) -> RoutingDecision:
    from app.services.department_ops import department_head

    admin = admin_reviewer(session)
    if requester.kind == "faculty":
        faculty = requester.faculty
        if requester.is_department_head:
            return route_hod_request(admin)
        hod = department_head(session, faculty.department_id)
        return route_faculty_request(
            faculty.id, Reviewer(hod.id, hod.full_name) if hod else None, faculty.department.code, admin,
        )
    student = requester.student
    affected = [Reviewer(p.assignment.faculty.id, p.assignment.faculty.full_name) for p in collected.affected]
    mentor = session.get(FacultyProfile, student.mentor_faculty_id) if student.mentor_faculty_id else None
    hod = department_head(session, student.department_id)
    return route_request(
        collected.request_type.value, affected, Reviewer(mentor.id, mentor.full_name) if mentor else None,
        Reviewer(hod.id, hod.full_name) if hod else None, student.department.code, admin,
    )


def admin_reviewer(session: Session) -> Optional[Reviewer]:
    """The administration, if any active admin account exists to decide requests."""
    exists = session.execute(
        select(AuthAccount.id).where(AuthAccount.role == UserRole.ADMIN, AuthAccount.is_active.is_(True))
    ).first()
    return ADMINISTRATION if exists is not None else None


def _history(request: WorkflowRequest, basis: str, reviewer: str, note: str, now: datetime) -> None:
    request.routing_history = [*(request.routing_history or []), {"at": now.isoformat(), "basis": basis, "reviewer": reviewer, "note": note}]


# ---------------------------------------------------------------------------
# Persistence and lifecycle
# ---------------------------------------------------------------------------


def _code() -> str:
    return f"REQ-{uuid.uuid4().hex[:8].upper()}"


def create_draft(
    session: Session, account: AuthAccount, requester: Requester, collected: Collected, routing: RoutingDecision,
    reason: str, now: datetime,
) -> WorkflowRequest:
    student, faculty = requester.student, requester.faculty
    request = WorkflowRequest(
        request_code=_code(), request_type=collected.request_type, status=WorkflowRequestStatus.DRAFT,
        requester_account_id=account.id, requester_role=account.role.value,
        student_id=student.student_code if student else None, requester_faculty_id=faculty.id if faculty else None,
        reviewer_faculty_id=routing.reviewer.faculty_id if routing.reviewer else None,
        reviewer_role="admin" if routing.reviewer and routing.reviewer.role == "admin" else None,
        department_id=(student or faculty).department_id, title=collected.title, reason=reason,
        context=collected.context.model_dump(mode="json"), routing_basis=routing.basis, routing_note=routing.note,
        created_at=now,
    )
    _history(request, routing.basis, routing.reviewer.name if routing.reviewer else "none", routing.note, now)
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


def _own(session: Session, request_code: str, account: AuthAccount) -> WorkflowRequest:
    request = get_request(session, request_code)
    if request is None or request.requester_account_id != account.id:
        raise RequestError("That request was not found.", status_code=404)
    return request


def _notify_student(session: Session, student_id: str, title: str, body: str, now: datetime) -> None:
    student = get_student_by_id(session, student_id)
    if student is not None:
        session.add(Notification(
            student_id=student.id, title=title, body=body, category="requests", status=NotificationStatus.SENT, sent_at=now,
        ))


def _notify_requester(session: Session, request: WorkflowRequest, title: str, body: str, now: datetime) -> None:
    if request.student_id:
        _notify_student(session, request.student_id, title, body, now)
    elif request.requester_faculty_id:
        staff_notifications.notify(session, request.requester_faculty_id, title, body, "requests", now=now)


def _subject(request: WorkflowRequest) -> str:
    label = REQUEST_TYPE_LABELS[request.request_type.value]
    context = request.context or {}
    event = context.get("event")
    if event:
        return f"{label} request for {event['title']}"
    if request.requester_faculty_id and context.get("request_date"):
        day = date.fromisoformat(context["request_date"])
        part = context.get("day_part")
        return f"{label} request for {day.day} {day.strftime('%B')}" + (f" ({part})" if part and part != "full_day" else "")
    return f"{label} request ({request.title.split(' for ', 1)[-1]})"


def _requester_name(session: Session, request: WorkflowRequest) -> str:
    if request.student_id:
        student = get_student_by_id(session, request.student_id)
        return student.user.full_name if student else request.student_id
    faculty = request.requester_faculty
    return faculty.full_name if faculty else "A faculty member"


def _reviewer_label(session: Session, request: WorkflowRequest, reviewer: FacultyProfile) -> str:
    from app.services.department_ops import department_head

    department = session.get(Department, request.department_id) if request.department_id else None
    head = department_head(session, request.department_id)
    if head is not None and head.id == reviewer.id and department is not None:
        return f"the HOD, {department.code} ({reviewer.full_name})"
    return reviewer.full_name


def submit(session: Session, account: AuthAccount, request_code: str, reason: Optional[str], now: datetime) -> WorkflowRequest:
    """The requester's explicit Confirm & Send: DRAFT -> PENDING (or NEEDS_REVIEW with no reviewer)."""
    request = _own(session, request_code, account)
    if request.status != WorkflowRequestStatus.DRAFT:
        raise RequestError(f"This request was already {request.status.value.replace('_', ' ')}.")
    if reason:
        request.reason = reason.strip()
    request.status = WorkflowRequestStatus.PENDING if (request.reviewer_faculty_id or request.reviewer_role == "admin") else WorkflowRequestStatus.NEEDS_REVIEW
    request.submitted_at = now
    if request.reviewer_role == "admin":
        _notify_requester(session, request, "Request sent", f"Your {_subject(request)} was sent to the Administration.", now)
        account_notifications.notify_admins(
            session, "Escalated request" if request.routing_basis == "admin_escalation" else "New request for the administration",
            f"{_requester_name(session, request)} ({request.requester_role}): {request.title}. {request.routing_note}",
            "admin_request", now=now, ref_key=f"request:{request.request_code}",
        )
    elif request.reviewer is not None:
        _notify_requester(session, request, "Request sent", f"Your {_subject(request)} was sent to {_reviewer_label(session, request, request.reviewer)}.", now)
        who = _requester_name(session, request)
        category = "escalated_request" if request.routing_basis == "hod_escalation" else "new_request"
        heading = "Escalated student request" if category == "escalated_request" else (
            "New faculty request" if request.requester_faculty_id else "New student request")
        staff_notifications.notify(
            session, request.reviewer.id, heading, f"{who}: {request.title}. {request.routing_note}", category, now=now,
            ref_key=f"request:{request.request_code}",
        )
    else:
        _notify_requester(session, request, "Request needs review", f"Your {_subject(request)} was submitted, but no reviewer could be determined. {request.routing_note}", now)
    operations_audit.record(
        session, event_type="request_submitted", actor_account_id=account.id, actor_role=account.role.value,
        subject_type="workflow_request", subject_id=request.request_code,
        message=f"Submitted to {_reviewer_name(request) or 'no reviewer (needs review)'}.",
        metadata={"status": request.status.value, "routing_basis": request.routing_basis}, at=now,
    )
    session.commit()
    return request


def cancel(session: Session, account: AuthAccount, request_code: str, now: datetime) -> WorkflowRequest:
    request = _own(session, request_code, account)
    if request.status not in (WorkflowRequestStatus.DRAFT, WorkflowRequestStatus.PENDING, WorkflowRequestStatus.NEEDS_REVIEW):
        raise RequestError(f"This request was already {request.status.value.replace('_', ' ')}.")
    request.status = WorkflowRequestStatus.CANCELLED
    operations_audit.record(
        session, event_type="request_cancelled", actor_account_id=account.id, actor_role=account.role.value,
        subject_type="workflow_request", subject_id=request.request_code, message="Cancelled by the requester.", at=now,
    )
    session.commit()
    return request


def escalate_to_hod(session: Session, request: WorkflowRequest, hod: FacultyProfile, department: Department, now: datetime) -> None:
    """A submitted student request no faculty reviewer could take goes to the department HOD (staged)."""
    request.reviewer_faculty_id = hod.id
    request.status = WorkflowRequestStatus.PENDING
    request.routing_basis = "hod_escalation"
    request.routing_note = f"Escalated to {hod.full_name}, HOD, {department.code} (no faculty reviewer could be determined)."
    _history(request, "hod_escalation", hod.full_name, request.routing_note, now)
    _notify_student(session, request.student_id, "Request escalated", f"Your {_subject(request)} was escalated to the HOD, {department.code} ({hod.full_name}).", now)
    staff_notifications.notify(
        session, hod.id, "Escalated student request", f"{_requester_name(session, request)}: {request.title}. {request.routing_note}",
        "escalated_request", now=now, ref_key=f"request:{request.request_code}",
    )
    operations_audit.record(
        session, event_type="request_escalated", actor_account_id=None, actor_role="system",
        subject_type="workflow_request", subject_id=request.request_code, message=request.routing_note, at=now,
    )


def escalate_to_admin(session: Session, request: WorkflowRequest, now: datetime) -> None:
    """Phase 18: a submitted request nobody below could take goes to the administration (staged)."""
    request.reviewer_faculty_id = None
    request.reviewer_role = "admin"
    request.status = WorkflowRequestStatus.PENDING
    request.routing_basis = "admin_escalation"
    request.routing_note = "Escalated to the Administration (no reviewer could be determined at a lower level)."
    _history(request, "admin_escalation", ADMINISTRATION.name, request.routing_note, now)
    _notify_requester(session, request, "Request escalated", f"Your {_subject(request)} was escalated to the Administration.", now)
    account_notifications.notify_admins(
        session, "Escalated request", f"{_requester_name(session, request)} ({request.requester_role}): {request.title}. {request.routing_note}",
        "admin_request", now=now, ref_key=f"request:{request.request_code}",
    )
    operations_audit.record(
        session, event_type="request_escalated", actor_account_id=None, actor_role="system",
        subject_type="workflow_request", subject_id=request.request_code, message=request.routing_note, at=now,
        metadata={"to": "admin"},
    )


def escalate_unassigned_to_admin(session: Session, now: datetime) -> int:
    """Every submitted NEEDS_REVIEW request with no reviewer goes to the administration (idempotent)."""
    if admin_reviewer(session) is None:
        return 0
    rows = session.execute(select(WorkflowRequest).where(
        WorkflowRequest.status == WorkflowRequestStatus.NEEDS_REVIEW, WorkflowRequest.reviewer_faculty_id.is_(None),
        WorkflowRequest.submitted_at.is_not(None),
    )).scalars().all()
    for request in rows:
        escalate_to_admin(session, request, now)
    if rows:
        session.commit()
    return len(rows)


def decide_as_admin(
    session: Session, account: AuthAccount, request_code: str, approve: bool, comment: Optional[str], now: datetime,
) -> WorkflowRequest:
    """Only requests routed to the administration, only PENDING, once, never one's own."""
    request = get_request(session, request_code)
    if request is None or request.submitted_at is None:
        raise RequestError("That request was not found.", status_code=404)
    if request.requester_account_id == account.id:
        raise RequestError("You cannot decide your own request.", status_code=403)
    if request.reviewer_role != "admin":
        raise RequestError("This request is not routed to the administration.", status_code=403)
    if request.status != WorkflowRequestStatus.PENDING:
        raise RequestError(f"This request was already {request.status.value.replace('_', ' ')}.")
    request.status = WorkflowRequestStatus.APPROVED if approve else WorkflowRequestStatus.REJECTED
    request.decided_at = now
    request.decided_by_account_id = account.id
    request.decision_reason = comment.strip() if comment and comment.strip() else None
    verb = "approved" if approve else "rejected"
    body = f"Your {_subject(request)} was {verb} by the Administration ({account.display_name})."
    if request.decision_reason:
        body += f" Comment: {request.decision_reason}"
    _notify_requester(session, request, f"Request {verb}", body, now)
    operations_audit.record(
        session, event_type=f"request_{verb}", actor_account_id=account.id, actor_role=account.role.value,
        subject_type="workflow_request", subject_id=request.request_code, message=body,
        metadata={"comment": request.decision_reason, "reviewer": "admin"}, at=now,
    )
    session.commit()
    return request


def list_for_admin(session: Session) -> List[WorkflowRequest]:
    return list(session.execute(
        select(WorkflowRequest).where(WorkflowRequest.reviewer_role == "admin", WorkflowRequest.submitted_at.is_not(None))
        .order_by(WorkflowRequest.id.desc())
    ).scalars().all())


def _reviewer_name(request: WorkflowRequest) -> Optional[str]:
    if request.reviewer_role == "admin":
        return "the Administration"
    return request.reviewer.full_name if request.reviewer else None


def decide(
    session: Session, account: AuthAccount, faculty: FacultyProfile, request_code: str, approve: bool,
    comment: Optional[str], now: datetime,
) -> WorkflowRequest:
    """Only the routed reviewer may decide, only a PENDING request, exactly once, never their own."""
    request = get_request(session, request_code)
    if request is None or request.submitted_at is None:  # drafts (and discarded drafts) were never sent
        raise RequestError("That request was not found.", status_code=404)
    if request.requester_account_id == account.id or request.requester_faculty_id == faculty.id:
        raise RequestError("You cannot decide your own request.", status_code=403)
    if request.reviewer_faculty_id != faculty.id:
        raise RequestError("This request is not assigned to you.", status_code=403)
    if request.status != WorkflowRequestStatus.PENDING:
        raise RequestError(f"This request was already {request.status.value.replace('_', ' ')}.")
    request.status = WorkflowRequestStatus.APPROVED if approve else WorkflowRequestStatus.REJECTED
    request.decided_at = now
    request.decided_by_account_id = account.id
    request.decision_reason = comment.strip() if comment and comment.strip() else None
    verb = "approved" if approve else "rejected"
    decider = _reviewer_label(session, request, faculty)
    if request.requester_faculty_id and decider.startswith("the HOD"):
        decider = decider.split(" (", 1)[0]  # "the HOD, CSE"
    body = f"Your {_subject(request)} was {verb} by {decider}."
    if request.decision_reason:
        body += f" Comment: {request.decision_reason}"
    _notify_requester(session, request, f"Request {verb}", body, now)
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


def list_own_faculty(session: Session, faculty: FacultyProfile) -> List[WorkflowRequest]:
    """A faculty member's own requests, including drafts they have not sent yet (not discarded ones)."""
    rows = session.execute(
        select(WorkflowRequest).where(WorkflowRequest.requester_faculty_id == faculty.id).order_by(WorkflowRequest.id.desc())
    ).scalars().all()
    return [r for r in rows if not (r.status == WorkflowRequestStatus.CANCELLED and r.submitted_at is None)]


def view(session: Session, request: WorkflowRequest) -> WorkflowRequestView:
    student = get_student_by_id(session, request.student_id) if request.student_id else None
    department = session.get(Department, request.department_id) if request.department_id else None
    decider = session.get(AuthAccount, request.decided_by_account_id) if request.decided_by_account_id else None
    return WorkflowRequestView(
        request_id=request.request_code, request_type=request.request_type.value,
        type_label=REQUEST_TYPE_LABELS[request.request_type.value], status=request.status.value, title=request.title,
        reason=request.reason, student_id=request.student_id, student_name=student.user.full_name if student else None,
        requester_kind="faculty" if request.requester_faculty_id else "student", requester_name=_requester_name(session, request),
        requester_role=request.requester_role, reviewer_role=request.reviewer_role or ("faculty" if request.reviewer_faculty_id else None),
        department_code=department.code if department else None, routing_history=list(request.routing_history or []),
        reviewer_name=_reviewer_name(request), routing_basis=request.routing_basis,
        routing_note=request.routing_note, context=RequestContext.model_validate(request.context or {}),
        created_at=request.created_at, submitted_at=request.submitted_at, decided_at=request.decided_at,
        decided_by=decider.display_name if decider else None, decision_reason=request.decision_reason,
    )


def student_record(session: Session, student_id: str) -> Optional[Student]:
    return get_student_by_id(session, student_id)


def threshold(knowledge: KnowledgeService, now: datetime) -> Optional[float]:
    return required_percentage(knowledge, now)
