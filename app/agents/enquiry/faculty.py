"""Faculty-scoped agent chat (Phase 16): Academic, Enquiry and Permission agents for faculty.

    question -> classify (LLM, structured ``FacultyQueryPlan``)
    -> resolve the class against the faculty member's OWN teaching assignments
    -> answer in code from attendance sessions, marks, counters and requests

Read-only. Access control is structural, not a prompt: every lookup starts
from ``assignments_of(faculty)`` or ``list_for_reviewer(faculty)``, so a
course or section the faculty member does not teach simply resolves to
nothing ("not one of your classes"). Every count and list is computed here
from database rows; the attendance threshold comes from retrieved policy
evidence. The LLM never sees the data and never writes the answer.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.faculty import AttendanceSessionStatus, FacultyProfile, SessionAttendanceMark, TeachingAssignment
from app.db.models.workflow import WorkflowRequestStatus
from app.llm.base import LLMProvider
from app.rules.attendance import compute_attendance
from app.rules.attendance_standing import AttendanceStanding, attendance_standing
from app.rules.class_session import pick_current, tally
from app.rules.policy_threshold import extract_attendance_threshold
from app.schemas.agent_chat import AgentQueryResponse
from app.schemas.faculty import FacultyQueryPlan
from app.schemas.workflow import REQUEST_TYPE_LABELS
from app.services import workflow_requests
from app.services.class_schedule import PlannedClass, clock, local, planned_classes, roster
from app.services.faculty_ops import assignments_of, course_counters
from app.services.knowledge import KnowledgeService

FACULTY_AGENT_KEYS = ("academic", "enquiry", "permission")
FACULTY_DISPLAY_NAMES = {"academic": "Academic Agent", "enquiry": "Enquiry Agent", "permission": "Permission Agent"}
_ACADEMIC_INTENTS = {"classes_today", "current_class", "next_class", "class_attendance", "absent_students", "below_threshold"}
ALLOWED_INTENTS = {
    "academic": _ACADEMIC_INTENTS,
    "permission": {"pending_requests"},
    "enquiry": _ACADEMIC_INTENTS | {"pending_requests"},
}
_STOP = {"of", "and", "the", "for", "in", "to"}


def _label(a: TeachingAssignment) -> str:
    return f"{a.course.title} ({a.department.code} Year {a.year}, Section {a.section})"


def course_matches(reference: str, title: str, code: str) -> bool:
    """Code, title, title substring, or an abbreviation of the title (e.g. OS, DBMS).

    An abbreviation matches when its letters appear in order in the title,
    starting at the title's first letter and including every significant
    word's initial.
    """
    ref = reference.strip().lower()
    if not ref:
        return False
    if ref in (code.lower(), title.lower()) or (len(ref) >= 4 and ref in title.lower()):
        return True
    if not re.fullmatch(r"[a-z]{2,6}", ref):
        return False
    words = [w.lower() for w in re.findall(r"[A-Za-z]+", title) if w.lower() not in _STOP]
    text = "".join(words)
    initial_positions, pos = set(), 0
    for w in words:
        initial_positions.add(pos)
        pos += len(w)
    if not text or ref[0] != text[0]:
        return False
    i, used = 0, []
    for ch in ref:
        j = text.find(ch, i)
        if j < 0:
            return False
        used.append(j)
        i = j + 1
    return initial_positions <= set(used)


class FacultyAgent:
    def __init__(self, *, llm_provider: LLMProvider, knowledge: KnowledgeService) -> None:
        self._llm = llm_provider
        self._knowledge = knowledge

    # -- entry point -------------------------------------------------------

    def handle(self, session: Session, faculty: FacultyProfile, agent_key: str, message: str, now: datetime) -> AgentQueryResponse:
        plan: FacultyQueryPlan = self._llm.plan_faculty_query(message)
        allowed = ALLOWED_INTENTS[agent_key]
        facts: Dict[str, object] = {"scope": "faculty", "plan": plan.model_dump(mode="json")}
        if plan.intent == "unknown":
            return self._reply(agent_key, "not_applicable", self._help(agent_key), facts)
        if plan.intent not in allowed:
            other = "Permission Agent" if plan.intent == "pending_requests" else "Academic Agent"
            return self._reply(agent_key, "not_applicable", f"That is a question for the {other}.", facts)

        assignments = assignments_of(session, faculty)
        if not assignments:
            return self._reply(agent_key, "verified", "You have no teaching assignments this term.", facts)
        if plan.intent == "pending_requests":
            return self._pending(session, faculty, agent_key, facts)

        targets, problem = self._targets(plan, assignments)
        if problem:
            return self._reply(agent_key, "not_applicable", problem, facts)
        today = planned_classes(session, targets, local(now).date())
        if plan.intent == "classes_today":
            return self._classes_today(session, today, agent_key, facts, now)
        if plan.intent in ("current_class", "next_class"):
            return self._current_or_next(session, today, plan.intent, agent_key, facts, now)
        if plan.intent in ("class_attendance", "absent_students"):
            return self._session_attendance(session, today, targets, plan.intent, agent_key, facts, now)
        return self._below_threshold(session, targets, agent_key, facts, now)

    # -- helpers ------------------------------------------------------------

    def _reply(self, agent_key: str, status: str, answer: str, facts: Dict[str, object], evidence=None) -> AgentQueryResponse:
        return AgentQueryResponse(
            agent_key=agent_key, display_name=FACULTY_DISPLAY_NAMES[agent_key], verification_status=status,
            answer=answer, facts=facts, evidence=list(evidence or []), live_ai=bool(getattr(self._llm, "is_live", False)),
        )

    @staticmethod
    def _help(agent_key: str) -> str:
        if agent_key == "permission":
            return "I can list the student requests waiting for your decision. Try: Which student requests are pending?"
        return (
            "I can answer about your own classes: today's classes, the class in session, who is present or absent, "
            "and who is below the attendance requirement."
        )

    @staticmethod
    def _targets(plan: FacultyQueryPlan, assignments: Sequence[TeachingAssignment]) -> Tuple[List[TeachingAssignment], Optional[str]]:
        found = list(assignments)
        if plan.course_reference:
            found = [a for a in found if course_matches(plan.course_reference, a.course.title, a.course.code)]
            if not found:
                return [], f"{plan.course_reference} is not one of your assigned classes, so I can't show its records."
        if plan.section_reference:
            found = [a for a in found if a.section.upper() == plan.section_reference.upper()]
        if plan.year_reference:
            found = [a for a in found if a.year == plan.year_reference]
        if not found:
            return [], "You are not assigned to teach that section, so I can't show its records."
        return found, None

    @staticmethod
    def _table(title: str, columns: List[str], rows: List[List[str]]) -> Dict[str, object]:
        return {"title": title, "columns": columns, "rows": rows}

    def _classes_today(self, session: Session, today: List[PlannedClass], agent_key: str, facts, now) -> AgentQueryResponse:
        live = [p for p in today if p.status != AttendanceSessionStatus.CANCELLED.value]
        weekday = local(now).strftime("%A")
        if not live:
            return self._reply(agent_key, "verified", f"You have no classes today ({weekday}).", facts)
        facts["table"] = self._table("Today's classes", ["Time", "Class", "Room", "Status"], [
            [f"{clock(p.start)}–{clock(p.end)}", _label(p.assignment), p.room, p.status.title()] for p in today
        ])
        noun = "class" if len(live) == 1 else "classes"
        return self._reply(agent_key, "verified", f"You have {len(live)} {noun} today ({weekday}).", facts)

    def _current_or_next(self, session: Session, today: List[PlannedClass], intent: str, agent_key: str, facts, now) -> AgentQueryResponse:
        index = pick_current(((p.status, p.start, p.end, p.session.actual_started_at if p.session else None) for p in today), now)
        current = today[index] if index is not None else None
        if intent == "next_class":
            upcoming = next((p for p in today if p.start > now and p is not current and p.status == "scheduled"), None)
            if upcoming is None:
                return self._reply(agent_key, "verified", "You have no more classes scheduled today.", facts)
            return self._reply(agent_key, "verified", f"Your next class is {_label(upcoming.assignment)} at {clock(upcoming.start)} in {upcoming.room}.", facts)
        if current is None:
            return self._reply(agent_key, "verified", "None of your classes is scheduled or in session right now.", facts)
        name = _label(current.assignment)
        if current.status == "active":
            t = self._tally(session, current)
            text = (f"{name} is in session in {current.room}; you started it at {clock(current.session.actual_started_at)}. "
                    f"{t.present + t.late} of {t.roster} students are marked present so far.")
        elif current.status == "scheduled":
            text = f"{name} is scheduled now ({clock(current.start)}–{clock(current.end)}, {current.room}) but has not been started."
        elif current.status == "closed":
            text = f"{name} ({clock(current.start)}–{clock(current.end)}) was closed at {clock(current.session.actual_closed_at)}."
        else:
            text = f"{name} ({clock(current.start)}–{clock(current.end)}) was cancelled."
        return self._reply(agent_key, "verified", text, facts)

    @staticmethod
    def _marks(session: Session, item: PlannedClass) -> Dict[int, str]:
        rows = session.execute(select(SessionAttendanceMark).where(SessionAttendanceMark.session_id == item.session.id)).scalars().all()
        return {m.student_id: m.status.value for m in rows}

    def _tally(self, session: Session, item: PlannedClass):
        return tally(len(roster(session, item.assignment)), self._marks(session, item).values())

    def _session_attendance(
        self, session: Session, today: List[PlannedClass], targets: List[TeachingAssignment], intent: str, agent_key: str, facts, now,
    ) -> AgentQueryResponse:
        started = [p for p in today if p.session is not None and p.status in ("active", "closed")]
        index = pick_current(((p.status, p.start, p.end, p.session.actual_started_at) for p in started), now)
        chosen = started[index] if index is not None else (max(started, key=lambda p: p.session.actual_started_at) if started else None)
        if chosen is None:
            which = _label(targets[0]) if len(targets) == 1 else "any of your classes"
            return self._reply(agent_key, "verified", f"No session of {which} has been started today, so no attendance has been taken.", facts)
        students = roster(session, chosen.assignment)
        marks = self._marks(session, chosen)
        t = tally(len(students), marks.values())
        name = f"{_label(chosen.assignment)}, {clock(chosen.start)}"
        if intent == "class_attendance":
            text = (f"In {name}: {t.present} present, {t.late} late, {t.absent} absent, {t.excused} excused"
                    f"{f', {t.unmarked} not yet marked' if t.unmarked else ''} ({t.roster} students).")
            facts["table"] = self._table("Attendance", ["Status", "Students"], [
                ["Present", str(t.present)], ["Late", str(t.late)], ["Absent", str(t.absent)],
                ["Excused", str(t.excused)], ["Not marked", str(t.unmarked)],
            ])
            return self._reply(agent_key, "verified", text, facts)
        absent = [s for s in students if marks.get(s.id) == "absent"]
        unmarked = [s for s in students if s.id not in marks]
        if not absent and not unmarked:
            return self._reply(agent_key, "verified", f"Nobody is absent in {name}.", facts)
        parts = []
        if absent:
            parts.append(f"{len(absent)} absent: {', '.join(s.user.full_name for s in absent)}")
        if unmarked:
            parts.append(f"{len(unmarked)} not yet marked: {', '.join(s.user.full_name for s in unmarked)}")
        facts["table"] = self._table("Absent or not marked", ["Student", "ID", "Status"], [
            [s.user.full_name, s.student_code, "Absent"] for s in absent
        ] + [[s.user.full_name, s.student_code, "Not marked"] for s in unmarked])
        return self._reply(agent_key, "verified", f"In {name}: " + "; ".join(parts) + ".", facts)

    def _below_threshold(self, session: Session, targets: List[TeachingAssignment], agent_key: str, facts, now) -> AgentQueryResponse:
        evidence = self._knowledge.get_active_policy("attendance_policy", as_of=now.date(), visibility="public")
        threshold = extract_attendance_threshold(evidence)
        if threshold.required_percentage is None:
            return self._reply(agent_key, "needs_review", "The attendance requirement could not be verified from the attendance policy, so I can't list students below it.", facts)
        required = threshold.required_percentage
        cited = [e for e in evidence if e.document_id == threshold.document_id]
        rows: List[List[str]] = []
        lines: List[str] = []
        for assignment in targets:
            below = []
            for student in roster(session, assignment):
                counters = course_counters(session, assignment, student)
                if counters is None:
                    continue
                calc = compute_attendance(counters.classes_attended, counters.classes_conducted, Decimal(required))
                if attendance_standing(calc) == AttendanceStanding.BELOW_REQUIREMENT:
                    below.append((student, calc, counters))
            if below:
                lines.append(f"{_label(assignment)}: " + ", ".join(f"{s.user.full_name} ({c.current_percentage}%)" for s, c, _ in below) + ".")
            else:
                lines.append(f"{_label(assignment)}: nobody is below {required}%.")
            rows += [[s.user.full_name, s.student_code, assignment.course.code, f"{k.classes_attended}/{k.classes_conducted}", f"{c.current_percentage}%"] for s, c, k in below]
        if rows:
            facts["table"] = self._table(f"Below {required}% attendance", ["Student", "ID", "Course", "Attended", "Attendance"], rows)
        header = f"The attendance policy requires {required}% per course."
        return self._reply(agent_key, "verified", "\n".join([header, *lines]), facts, evidence=cited)

    def _pending(self, session: Session, faculty: FacultyProfile, agent_key: str, facts) -> AgentQueryResponse:
        pending = [r for r in workflow_requests.list_for_reviewer(session, faculty) if r.status == WorkflowRequestStatus.PENDING]
        if not pending:
            return self._reply(agent_key, "verified", "No student requests are waiting for your decision.", facts)
        facts["table"] = self._table("Pending student requests", ["Request", "Student", "Type", "Submitted"], [
            [r.title, (workflow_requests.view(session, r).student_name or r.student_id or ""), REQUEST_TYPE_LABELS[r.request_type.value],
             local(r.submitted_at).strftime("%d %b, %H:%M") if r.submitted_at else ""]
            for r in pending
        ])
        noun = "request is" if len(pending) == 1 else "requests are"
        return self._reply(agent_key, "verified", f"{len(pending)} student {noun} waiting for your decision. Open Student Requests to approve or reject.", facts)
