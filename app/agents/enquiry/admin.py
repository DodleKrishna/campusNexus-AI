"""Institution-scoped agent chat for administrators (Phase 18).

    question -> classify (LLM, structured ``AdminQueryPlan``)
    -> institution-wide deterministic services (``department_ops`` with an
       ``InstitutionScope``, ``admin_ops``)
    -> answer built in code from those results

Read-only. Only ADMIN accounts reach it (``current_admin``). An optional
department named in the question narrows the scope to that department; an
unknown code is reported, never guessed. Class state comes from
``class_state``; attendance from counters and ``compute_attendance``; the
threshold from retrieved policy evidence (attached); system health from the
mission audit trail. The LLM never computes or phrases a figure.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.auth import AuthAccount
from app.db.models.mission import AuditLog, Mission
from app.llm.base import LLMProvider
from app.rules.class_session import ClassState
from app.rules.policy_threshold import extract_attendance_threshold
from app.schemas.admin_console import AdminQueryPlan
from app.schemas.agent_chat import ActionHint, AgentQueryResponse
from app.schemas.department import DepartmentClassView
from app.schemas.enums import MissionStatus
from app.services import admin_ops, department_ops as ops
from app.services.admin_ops import AdminError
from app.services.knowledge import KnowledgeService
from app.services.workflow_requests import open_events

ADMIN_AGENT_KEYS = ("enquiry", "academic", "complaints", "permission", "events")
ADMIN_DISPLAY_NAMES = {
    "enquiry": "Enquiry Agent", "academic": "Academic Agent", "complaints": "Complaints Agent",
    "permission": "Permission Agent", "events": "Events Agent",
}
_ACADEMIC = {"classes_not_started", "classes_running", "classes_today", "attendance_risk_by_department", "below_threshold"}
ALLOWED_INTENTS = {
    "academic": _ACADEMIC,
    "complaints": {"complaints_breached"},
    "permission": {"pending_requests", "escalated_requests", "make_request"},
    "events": {"upcoming_events"},
    "enquiry": _ACADEMIC | {"complaints_breached", "pending_requests", "escalated_requests", "failed_workflows",
                            "upcoming_events", "campus_overview", "make_request"},
}
_OWNER = {**{i: "Academic Agent" for i in _ACADEMIC}, "complaints_breached": "Complaints Agent",
          "pending_requests": "Permission Agent", "escalated_requests": "Permission Agent", "make_request": "Permission Agent",
          "upcoming_events": "Events Agent", "failed_workflows": "Enquiry Agent", "campus_overview": "Enquiry Agent"}


def _line(v: DepartmentClassView) -> str:
    return f"{v.course_title} ({v.class_label}, {v.start_local}–{v.end_local}, {v.faculty_name})"


def _plural(n: int, word: str, plural: Optional[str] = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


class AdminAgent:
    def __init__(self, *, llm_provider: LLMProvider, knowledge: KnowledgeService) -> None:
        self._llm = llm_provider
        self._knowledge = knowledge

    def handle(self, session: Session, account: AuthAccount, agent_key: str, message: str, now: datetime) -> AgentQueryResponse:
        plan: AdminQueryPlan = self._llm.plan_admin_query(message)
        facts: Dict[str, object] = {"scope": "faculty", "plan": plan.model_dump(mode="json")}
        if plan.intent == "unknown":
            return self._reply(agent_key, "not_applicable", self._help(), facts)
        if plan.intent not in ALLOWED_INTENTS[agent_key]:
            return self._reply(agent_key, "not_applicable", f"That is a question for the {_OWNER[plan.intent]}.", facts)
        try:
            scope = admin_ops.institution_scope(session, account, plan.department_reference)
        except AdminError as exc:
            return self._reply(agent_key, "not_applicable", str(exc), facts)
        facts["departments"] = [d.code for d in scope.departments]
        where = scope.departments[0].code if plan.department_reference else "the campus"
        return getattr(self, f"_{plan.intent}")(session, scope, where, agent_key, facts, now)

    # -- plumbing ------------------------------------------------------------

    def _reply(self, key, status, answer, facts, evidence=None, hint: Optional[ActionHint] = None) -> AgentQueryResponse:
        return AgentQueryResponse(
            agent_key=key, display_name=ADMIN_DISPLAY_NAMES[key], verification_status=status, answer=answer, facts=facts,
            evidence=list(evidence or []), action_hint=hint, live_ai=bool(getattr(self._llm, "is_live", False)),
        )

    @staticmethod
    def _help() -> str:
        return ("I answer institution-wide questions from live records: classes running or not started, attendance risk by "
                "department, pending and escalated requests, complaints past SLA and failed agent workflows.")

    @staticmethod
    def _table(title: str, columns: List[str], rows: List[List[str]]) -> Dict[str, object]:
        return {"title": title, "columns": columns, "rows": rows}

    # -- operations ----------------------------------------------------------

    def _classes_not_started(self, session, scope, where, key, facts, now):
        views = ops.activity(session, scope, now)
        late = ops.not_started(views)
        grace = ops.start_grace_minutes()
        if not late:
            return self._reply(key, "verified", f"Every class due so far today across {where} has started (grace period {grace} minutes).", facts)
        facts["table"] = self._table("Classes not started", ["Class", "Group", "Time", "Faculty", "State"], [
            [v.course_title, v.class_label, f"{v.start_local}–{v.end_local}", v.faculty_name, "Delayed" if v.state == "delayed" else "Not held"] for v in late
        ])
        delayed = [v for v in late if v.state == ClassState.DELAYED.value]
        not_held = [v for v in late if v.state == ClassState.NOT_HELD.value]
        lines = [f"{_plural(len(late), 'class', 'classes')} across {where} {'have' if len(late) != 1 else 'has'} not started today."]
        if delayed:
            lines.append(f"Still within their scheduled time, {grace}+ minutes late: " + "; ".join(_line(v) for v in delayed) + ".")
        if not_held:
            lines.append("Scheduled time passed without starting: " + "; ".join(_line(v) for v in not_held) + ".")
        return self._reply(key, "verified", "\n".join(lines), facts)

    def _classes_running(self, session, scope, where, key, facts, now):
        running = [v for v in ops.activity(session, scope, now) if v.state == ClassState.ACTIVE.value]
        if not running:
            return self._reply(key, "verified", f"No classes are running right now across {where}.", facts)
        return self._reply(key, "verified", f"{_plural(len(running), 'class', 'classes')} running now: " + "; ".join(_line(v) for v in running) + ".", facts)

    def _classes_today(self, session, scope, where, key, facts, now):
        views = [v for v in ops.activity(session, scope, now) if v.state != ClassState.CANCELLED.value]
        by_dept = Counter(v.class_label.split(" ")[0] for v in views)
        detail = ", ".join(f"{code} {n}" for code, n in sorted(by_dept.items())) or "none"
        return self._reply(key, "verified", f"{_plural(len(views), 'class', 'classes')} scheduled today across {where} ({detail}).", facts)

    def _threshold(self, now: datetime):
        evidence = self._knowledge.get_active_policy("attendance_policy", as_of=now.date(), visibility="public")
        threshold = extract_attendance_threshold(evidence)
        return threshold, [e for e in evidence if e.document_id == threshold.document_id]

    def _attendance_risk_by_department(self, session, scope, where, key, facts, now):
        threshold, cited = self._threshold(now)
        if threshold.required_percentage is None:
            return self._reply(key, "needs_review", "The attendance requirement could not be verified from the attendance policy.", facts)
        rows = sorted(admin_ops.attendance(session, self._knowledge, scope, now).by_department,
                      key=lambda d: (-d.students_below_threshold, d.code))
        facts["table"] = self._table(f"Students below {threshold.required_percentage}% in a course", ["Department", "Students", "At risk", "Attendance"], [
            [d.code, str(d.students), str(d.students_below_threshold), f"{d.percentage}%" if d.percentage is not None else "—"] for d in rows
        ])
        top = rows[0] if rows else None
        if top is None or top.students_below_threshold == 0:
            return self._reply(key, "verified", f"No student is below {threshold.required_percentage}% in any course across {where}.", facts, cited)
        ranking = ", ".join(f"{d.code} {d.students_below_threshold}" for d in rows)
        return self._reply(key, "verified", f"{top.code} has the most attendance-risk students ({top.students_below_threshold} below "
                           f"{threshold.required_percentage}% in at least one course). By department: {ranking}.", facts, cited)

    def _below_threshold(self, session, scope, where, key, facts, now):
        threshold, cited = self._threshold(now)
        if threshold.required_percentage is None:
            return self._reply(key, "needs_review", "The attendance requirement could not be verified from the attendance policy.", facts)
        insights = ops.attendance_insights(session, self._knowledge, scope, now)
        students = sorted({e.student_id for e in insights.low_attendance})
        facts["table"] = self._table(f"Below {threshold.required_percentage}%", ["Student", "Group", "Course", "Attendance"], [
            [e.full_name, e.class_label, e.course_code, f"{e.percentage}%"] for e in insights.low_attendance
        ])
        return self._reply(key, "verified", f"{_plural(len(students), 'student')} across {where} {'are' if len(students) != 1 else 'is'} below "
                           f"{threshold.required_percentage}% in at least one course ({len(insights.low_attendance)} course enrolments).", facts, cited)

    # -- requests ------------------------------------------------------------

    def _pending_requests(self, session, scope, where, key, facts, now):
        rows = admin_ops.pending_everywhere(session, scope)
        if not rows:
            return self._reply(key, "verified", f"No requests are pending across {where}.", facts)
        holders = Counter("the Administration" if r.reviewer_role == "admin" else (r.reviewer.full_name if r.reviewer else "no reviewer (needs review)") for r in rows)
        facts["table"] = self._table("Pending requests", ["Request", "Requester role", "Status", "With"], [
            [r.title, r.requester_role, r.status.value.replace("_", " "), "the Administration" if r.reviewer_role == "admin" else (r.reviewer.full_name if r.reviewer else "—")] for r in rows
        ])
        return self._reply(key, "verified", f"{_plural(len(rows), 'request')} pending across {where}: " + ", ".join(f"{n} with {who}" for who, n in holders.most_common()) + ".", facts)

    def _escalated_requests(self, session, scope, where, key, facts, now):
        rows = [r for r in admin_ops.pending_everywhere(session, scope)
                if r.routing_basis in ("hod_escalation", "admin_escalation") or r.status.value == "needs_review"]
        if not rows:
            return self._reply(key, "verified", f"No escalated requests are open across {where}.", facts)
        return self._reply(key, "verified", f"{_plural(len(rows), 'escalated request')} open: " + "; ".join(f"{r.title} ({r.routing_basis.replace('_', ' ')})" for r in rows) + ".", facts)

    def _make_request(self, session, scope, where, key, facts, now):
        return self._reply(key, "not_applicable", "Administrator requests have no higher reviewer in CampusNexus, so there is no request workflow for them.", facts)

    # -- complaints / events / system -----------------------------------------

    def _complaints_breached(self, session, scope, where, key, facts, now):
        breached = [c for c in admin_ops.complaints(session, scope, now) if (c.response_breached or c.resolution_breached) and c.status in ("open", "in_progress")]
        if not breached:
            return self._reply(key, "verified", f"No open complaint across {where} has breached its SLA.", facts)
        facts["table"] = self._table("Complaints past SLA", ["Case", "Department", "Office", "Priority", "Breached"], [
            [c.case_code, c.student_department, c.office, c.priority, ", ".join(x for x, b in (("response", c.response_breached), ("resolution", c.resolution_breached)) if b)] for c in breached
        ])
        return self._reply(key, "verified", f"{_plural(len(breached), 'open complaint')} across {where} {'have' if len(breached) != 1 else 'has'} breached SLA: "
                           + ", ".join(c.case_code for c in breached) + ".", facts)

    def _failed_workflows(self, session, scope, where, key, facts, now):
        since = now - timedelta(hours=24)
        events = session.execute(select(AuditLog).where(AuditLog.event_type.in_(admin_ops.ERROR_EVENTS), AuditLog.timestamp >= since)).scalars().all()
        failed = session.execute(select(Mission).where(Mission.status == MissionStatus.FAILED, Mission.updated_at >= since)).scalars().all()
        if not events and not failed:
            return self._reply(key, "verified", "No failed missions, agent errors or provider outages were recorded in the last 24 hours.", facts)
        kinds = Counter(e.event_type.replace("_", " ") for e in events)
        detail = ", ".join(f"{n} {kind}" for kind, n in kinds.most_common())
        return self._reply(key, "verified", f"In the last 24 hours: {_plural(len(failed), 'failed mission')}" + (f"; audit events: {detail}" if detail else "") + ".", facts)

    def _upcoming_events(self, session, scope, where, key, facts, now):
        events = [e for e in open_events(session, now) if e.start_at <= now + timedelta(days=14)]
        return self._reply(key, "verified", f"{_plural(len(events), 'campus event')} in the next 14 days" + (": " + ", ".join(e.title for e in events) if events else "") + ".", facts)

    def _campus_overview(self, session, scope, where, key, facts, now):
        dash = admin_ops.dashboard(session, scope, now)
        late = ops.not_started(dash.activity)
        risk = admin_ops.attendance(session, self._knowledge, scope, now)
        errors = dash.system_errors_24h
        lines = [
            f"Classes: {dash.classes_today} today — {dash.active_classes} running, {dash.completed_classes} completed, "
            f"{dash.not_started_classes} not started, {dash.cancelled_classes} cancelled.",
        ]
        if late:
            lines.append("Not started: " + "; ".join(_line(v) for v in late) + ".")
        lines.append(f"Attendance: {risk.low_attendance_students} students below {risk.required_percentage:g}% in a course"
                     + (f"; {len(risk.insights.incomplete_sessions)} live sessions with unmarked students." if risk.insights.incomplete_sessions else "."))
        lines.append(f"Requests: {dash.pending_requests} pending, {dash.escalations} escalated or needing review.")
        lines.append(f"Complaints: {dash.sla_breaches} open complaints past SLA.")
        lines.append(f"System: {errors} agent/provider error{'s' if errors != 1 else ''} in the last 24 hours.")
        normal = not late and dash.sla_breaches == 0 and errors == 0 and dash.escalations == 0
        head = f"{where[0].upper() + where[1:]} is running normally today." if normal else f"{where[0].upper() + where[1:]} needs attention today."
        facts["consulted_areas"] = ["operations", "attendance", "requests", "complaints", "system"]
        return self._reply(key, "verified", "\n".join([head, *lines]), facts)
