"""HOD-scoped agent chat (Phase 17): Academic, Enquiry, Permission, Complaints and Events for a department head.

    question -> classify (LLM, structured ``HodQueryPlan``)
    -> department-scoped deterministic services (``app/services/department_ops.py``)
    -> answer built in code from those verified results

Read-only. The department comes only from the caller's ``HodScope`` (resolved
server-side from the account's faculty link and ``departments.hod_faculty_id``);
the plan has no department field, so a question about another department can
only ever be answered with this department's data. Class state (delayed / not
held) comes from ``class_state`` with the configured grace period; attendance
from counters and ``compute_attendance``; the threshold from retrieved policy
evidence, which is attached.

The Enquiry Agent's broad check ("is everything running normally?") consults
each area in turn and lists only what each verified service returned.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.db.models.events import EventRegistration, RegistrationStatus
from app.db.models.identity import Student
from app.llm.base import LLMProvider
from app.rules.class_session import ClassState
from app.rules.policy_threshold import extract_attendance_threshold
from app.schemas.agent_chat import ActionHint, AgentQueryResponse
from app.schemas.department import DepartmentClassView, HodQueryPlan
from app.schemas.workflow import REQUEST_TYPE_LABELS
from app.services import department_ops as ops
from app.services.class_schedule import clock, local
from app.services.knowledge import KnowledgeService
from app.services.workflow_requests import open_events

HOD_AGENT_KEYS = ("academic", "enquiry", "permission", "complaints", "events")
HOD_DISPLAY_NAMES = {
    "academic": "Academic Agent", "enquiry": "Enquiry Agent", "permission": "Permission Agent",
    "complaints": "Complaints Agent", "events": "Events Agent",
}
_ACADEMIC = {"classes_running", "classes_today", "faculty_teaching_today", "classes_not_started", "below_threshold",
             "lowest_course_attendance", "section_present_today"}
ALLOWED_INTENTS = {
    "academic": _ACADEMIC,
    "permission": {"pending_faculty_requests", "escalated_student_requests", "make_request"},
    "complaints": {"complaints_breached"},
    "events": {"department_events"},
    "enquiry": _ACADEMIC | {"pending_faculty_requests", "escalated_student_requests", "complaints_breached",
                            "department_events", "department_overview", "make_request"},
}
_OWNER = {**{i: "Academic Agent" for i in _ACADEMIC}, "pending_faculty_requests": "Permission Agent",
          "escalated_student_requests": "Permission Agent", "make_request": "Permission Agent",
          "complaints_breached": "Complaints Agent", "department_events": "Events Agent",
          "department_overview": "Enquiry Agent"}


def _class_line(v: DepartmentClassView) -> str:
    return f"{v.course_title} ({v.class_label}, {v.start_local}–{v.end_local}, {v.faculty_name})"


class HodAgent:
    def __init__(self, *, llm_provider: LLMProvider, knowledge: KnowledgeService) -> None:
        self._llm = llm_provider
        self._knowledge = knowledge

    def handle(self, session: Session, scope: ops.HodScope, agent_key: str, message: str, now: datetime) -> AgentQueryResponse:
        plan: HodQueryPlan = self._llm.plan_hod_query(message)
        facts: Dict[str, object] = {"scope": "faculty", "department": scope.department.code, "plan": plan.model_dump(mode="json")}
        if plan.intent == "unknown":
            return self._reply(agent_key, "not_applicable", self._help(scope), facts)
        if plan.intent not in ALLOWED_INTENTS[agent_key]:
            return self._reply(agent_key, "not_applicable", f"That is a question for the {_OWNER[plan.intent]}.", facts)
        handler = getattr(self, f"_{plan.intent}")
        return handler(session, scope, plan, agent_key, facts, now)

    # -- plumbing ------------------------------------------------------------

    def _reply(self, agent_key, status, answer, facts, evidence=None, hint: Optional[ActionHint] = None) -> AgentQueryResponse:
        return AgentQueryResponse(
            agent_key=agent_key, display_name=HOD_DISPLAY_NAMES[agent_key],
            verification_status=status, answer=answer, facts=facts, evidence=list(evidence or []), action_hint=hint,
            live_ai=bool(getattr(self._llm, "is_live", False)),
        )

    @staticmethod
    def _help(scope: ops.HodScope) -> str:
        return (f"I answer questions about {scope.department.code} from live records: classes running or not started, "
                "attendance below the requirement, faculty requests, escalated student requests and complaints.")

    @staticmethod
    def _table(title: str, columns: List[str], rows: List[List[str]]) -> Dict[str, object]:
        return {"title": title, "columns": columns, "rows": rows}

    def _threshold(self, now: datetime):
        evidence = self._knowledge.get_active_policy("attendance_policy", as_of=now.date(), visibility="public")
        threshold = extract_attendance_threshold(evidence)
        return threshold, [e for e in evidence if e.document_id == threshold.document_id]

    # -- academic / operations ------------------------------------------------

    def _classes_running(self, session, scope, plan, key, facts, now):
        running = [v for v in ops.activity(session, scope, now) if v.state == ClassState.ACTIVE.value]
        if not running:
            return self._reply(key, "verified", f"No {scope.department.code} classes are running right now.", facts)
        facts["table"] = self._table("Running now", ["Class", "Group", "Faculty", "Started", "Present"], [
            [v.course_title, v.class_label, v.faculty_name, clock(v.actual_started_at) if v.actual_started_at else "",
             f"{v.tally.present + v.tally.late}/{v.tally.roster}" if v.tally else ""] for v in running
        ])
        noun = "class is" if len(running) == 1 else "classes are"
        return self._reply(key, "verified", f"{len(running)} {scope.department.code} {noun} running now: " + "; ".join(_class_line(v) for v in running) + ".", facts)

    def _classes_today(self, session, scope, plan, key, facts, now):
        views = [v for v in ops.activity(session, scope, now) if v.state != ClassState.CANCELLED.value]
        facts["table"] = self._table("Today's classes", ["Time", "Class", "Group", "Faculty", "State"], [
            [f"{v.start_local}–{v.end_local}", v.course_title, v.class_label, v.faculty_name, v.state.replace("_", " ").title()] for v in views
        ])
        return self._reply(key, "verified", f"{scope.department.code} has {len(views)} class{'es' if len(views) != 1 else ''} scheduled today ({local(now):%A}).", facts)

    def _faculty_teaching_today(self, session, scope, plan, key, facts, now):
        views = [v for v in ops.activity(session, scope, now) if v.state != ClassState.CANCELLED.value]
        names = sorted({v.faculty_name for v in views})
        if not names:
            return self._reply(key, "verified", f"No {scope.department.code} faculty member teaches today.", facts)
        return self._reply(key, "verified", f"{len(names)} faculty member{'s' if len(names) != 1 else ''} teach today: {', '.join(names)}.", facts)

    def _classes_not_started(self, session, scope, plan, key, facts, now):
        views = ops.activity(session, scope, now)
        grace = ops.start_grace_minutes()
        delayed = [v for v in views if v.state == ClassState.DELAYED.value]
        not_held = [v for v in views if v.state == ClassState.NOT_HELD.value]
        if not delayed and not not_held:
            return self._reply(key, "verified", f"Every {scope.department.code} class due so far today has started (grace period {grace} minutes).", facts)
        lines = []
        if delayed:
            lines.append(f"Not started {grace}+ minutes after the scheduled time: " + "; ".join(_class_line(v) for v in delayed) + ".")
        if not_held:
            lines.append("Scheduled time has passed without the class being started: " + "; ".join(_class_line(v) for v in not_held) + ".")
        facts["table"] = self._table("Classes not started", ["Class", "Group", "Time", "Faculty", "State"], [
            [v.course_title, v.class_label, f"{v.start_local}–{v.end_local}", v.faculty_name, "Delayed" if v.state == "delayed" else "Not held"]
            for v in delayed + not_held
        ])
        total = len(delayed) + len(not_held)
        head = f"{total} {scope.department.code} " + ("classes haven't" if total != 1 else "class hasn't") + " started today."
        return self._reply(key, "verified", "\n".join([head, *lines]), facts)

    def _below_threshold(self, session, scope, plan, key, facts, now):
        threshold, cited = self._threshold(now)
        if threshold.required_percentage is None:
            return self._reply(key, "needs_review", "The attendance requirement could not be verified from the attendance policy.", facts)
        insights = ops.attendance_insights(session, self._knowledge, scope, now)
        entries = [e for e in insights.low_attendance
                   if (plan.year_reference is None or e.class_label.split(" ")[1].split("-")[0] == str(plan.year_reference))
                   and (plan.section_reference is None or e.class_label.endswith(f"-{plan.section_reference.upper()}"))]
        req = threshold.required_percentage
        if not entries:
            return self._reply(key, "verified", f"No {scope.department.code} student is below {req}% in any course.", facts, cited)
        students = sorted({e.student_id for e in entries})
        facts["table"] = self._table(f"Below {req}% attendance", ["Student", "ID", "Group", "Course", "Attended", "Attendance"], [
            [e.full_name, e.student_id, e.class_label, e.course_code, f"{e.classes_attended}/{e.classes_conducted}", f"{e.percentage}%"] for e in entries
        ])
        listed = "; ".join(f"{e.full_name} — {e.course_code} {e.percentage}%" for e in entries)
        answer = (f"The attendance policy requires {req}% per course. {len(students)} {scope.department.code} student"
                  f"{'s are' if len(students) != 1 else ' is'} below it: {listed}.")
        return self._reply(key, "verified", answer, facts, cited)

    def _lowest_course_attendance(self, session, scope, plan, key, facts, now):
        insights = ops.attendance_insights(session, self._knowledge, scope, now)
        ranked = sorted((c for c in insights.courses if c.percentage is not None), key=lambda c: c.percentage)
        if not ranked:
            return self._reply(key, "verified", "No attendance has been recorded for the department's courses yet.", facts)
        low = ranked[0]
        facts["table"] = self._table("Course attendance (lowest first)", ["Course", "Group", "Faculty", "Attended", "Attendance"], [
            [c.course_title, c.class_label, c.faculty_name, f"{c.classes_attended}/{c.classes_conducted}", f"{c.percentage}%"] for c in ranked
        ])
        return self._reply(key, "verified", f"{low.course_title} ({low.class_label}, {low.faculty_name}) has the lowest attendance: {low.percentage}% ({low.classes_attended}/{low.classes_conducted} across {low.students} students).", facts)

    def _section_present_today(self, session, scope, plan, key, facts, now):
        sessions = ops.present_today(session, scope, now, plan.year_reference, plan.section_reference)
        group = f"{scope.department.code} {plan.year_reference or ''}{'-' + plan.section_reference.upper() if plan.section_reference else ''}".strip()
        if not sessions:
            return self._reply(key, "verified", f"No {group} class session has been started today, so nobody has been marked present yet.", facts)
        present = sum(s.tally.present + s.tally.late for s in sessions)
        marked = sum(s.tally.roster - s.tally.unmarked for s in sessions)
        facts["table"] = self._table("Sessions today", ["Class", "Group", "Start", "Present", "Absent", "Not marked"], [
            [s.course_title, s.class_label, s.start_local, str(s.tally.present + s.tally.late), str(s.tally.absent), str(s.tally.unmarked)] for s in sessions
        ])
        detail = "; ".join(f"{s.course_title} {s.start_local}: {s.tally.present + s.tally.late} of {s.tally.roster}" for s in sessions)
        return self._reply(key, "verified", f"{group} today: {present} present out of {marked} marked across {len(sessions)} session{'s' if len(sessions) != 1 else ''} ({detail}).", facts)

    # -- permission ----------------------------------------------------------

    def _pending_faculty_requests(self, session, scope, plan, key, facts, now):
        ops.escalate_unassigned(session, scope, now)
        pending = ops.pending_faculty_requests(session, scope)
        if not pending:
            return self._reply(key, "verified", "No faculty requests are waiting for your decision.", facts)
        facts["table"] = self._table("Pending faculty requests", ["Faculty", "Request", "Type", "Affected classes"], [
            [r.requester_faculty.full_name, r.title, REQUEST_TYPE_LABELS[r.request_type.value], str(len((r.context or {}).get("affected_classes") or []))]
            for r in pending
        ])
        return self._reply(key, "verified", f"{len(pending)} faculty request{'s are' if len(pending) != 1 else ' is'} waiting for your decision: "
                           + "; ".join(f"{r.requester_faculty.full_name} — {r.title}" for r in pending) + ".", facts)

    def _escalated_student_requests(self, session, scope, plan, key, facts, now):
        ops.escalate_unassigned(session, scope, now)
        escalated = ops.escalated_student_requests(session, scope)
        if not escalated:
            return self._reply(key, "verified", "No student requests have been escalated to you.", facts)
        return self._reply(key, "verified", f"{len(escalated)} escalated student request{'s' if len(escalated) != 1 else ''}: " + "; ".join(r.title for r in escalated) + ".", facts)

    def _make_request(self, session, scope, plan, key, facts, now):
        facts["route"] = "permission_request"
        facts["request_message"] = message
        hint = ActionHint(agent_key="permission", message="That is a request, so it goes through the Permission Agent: it prepares it for you to confirm, then sends it to the Administration.")
        return self._reply(key, "not_applicable", hint.message, facts, hint=hint)

    # -- complaints / events -------------------------------------------------

    def _complaints_breached(self, session, scope, plan, key, facts, now):
        cases = ops.complaints(session, scope, now)
        breached = [c for c in cases if (c.response_breached or c.resolution_breached) and c.status in ("open", "in_progress")]
        if not breached:
            return self._reply(key, "verified", f"No open complaint from a {scope.department.code} student has breached its SLA.", facts)
        facts["table"] = self._table("Complaints past SLA", ["Case", "Student", "Category", "Office", "Breached"], [
            [c.case_code, c.student_name, c.category, c.office, ", ".join(x for x, b in (("response", c.response_breached), ("resolution", c.resolution_breached)) if b)]
            for c in breached
        ])
        return self._reply(key, "verified", f"{len(breached)} open complaint{'s' if len(breached) != 1 else ''} from {scope.department.code} students "
                           f"{'have' if len(breached) != 1 else 'has'} breached SLA: " + ", ".join(c.case_code for c in breached) + ".", facts)

    def _department_events(self, session, scope, plan, key, facts, now):
        start, end = ops.upcoming_window(now)
        events = [e for e in open_events(session, now) if e.start_at <= end]
        ids = [s.id for s in ops.students(session, scope)]
        rows = []
        for e in events:
            registered = session.query(EventRegistration).join(Student, Student.id == EventRegistration.student_id).filter(
                EventRegistration.event_id == e.id, EventRegistration.status == RegistrationStatus.CONFIRMED, Student.id.in_(ids)).count()
            rows.append([e.title, local(e.start_at).strftime("%a %d %b, %H:%M"), e.location, str(registered)])
        if not rows:
            return self._reply(key, "verified", "No campus events are scheduled in the next 14 days.", facts)
        facts["table"] = self._table("Events in the next 14 days", ["Event", "When", "Where", f"{scope.department.code} students registered"], rows)
        return self._reply(key, "verified", f"{len(rows)} campus event{'s' if len(rows) != 1 else ''} in the next 14 days.", facts)

    # -- enquiry: the broad department check ----------------------------------

    def _department_overview(self, session, scope, plan, key, facts, now):
        dept = scope.department.code
        views = ops.activity(session, scope, now)
        live = [v for v in views if v.state != ClassState.CANCELLED.value]
        not_started = ops.not_started(views)
        insights = ops.attendance_insights(session, self._knowledge, scope, now)
        ops.escalate_unassigned(session, scope, now)
        pending = ops.pending_faculty_requests(session, scope)
        escalated = ops.escalated_student_requests(session, scope)
        breached = [c for c in ops.complaints(session, scope, now) if (c.response_breached or c.resolution_breached) and c.status in ("open", "in_progress")]
        lines = [
            f"Classes: {len(live)} today — {sum(1 for v in views if v.state == 'active')} running, "
            f"{sum(1 for v in views if v.state == 'completed')} completed, {len(not_started)} not started.",
        ]
        if not_started:
            lines.append("Not started: " + "; ".join(_class_line(v) for v in not_started) + ".")
        if insights.incomplete_sessions:
            lines.append(f"Sessions in progress with students not yet marked: {len(insights.incomplete_sessions)}.")
        students_below = sorted({e.student_id for e in insights.low_attendance})
        lines.append(f"Attendance: {len(students_below)} student{'s' if len(students_below) != 1 else ''} below "
                     f"{insights.required_percentage}% in at least one course." if insights.required_percentage is not None
                     else "Attendance: the requirement could not be verified from policy.")
        lines.append(f"Requests: {len(pending)} faculty request{'s' if len(pending) != 1 else ''} and {len(escalated)} escalated student request{'s' if len(escalated) != 1 else ''} waiting for you.")
        lines.append(f"Complaints: {len(breached)} open complaint{'s' if len(breached) != 1 else ''} from {dept} students past SLA.")
        normal = not not_started and not breached
        head = f"{dept} looks normal today." if normal else f"{dept} needs attention today."
        facts["consulted_areas"] = ["operations", "attendance", "requests", "complaints"]
        return self._reply(key, "verified", "\n".join([head, *lines]), facts)
