"""Nexus: the personal assistant on top of the AgentOS kernel (Phase 2).

authenticated human -> PersonalAssistant -> persistent mission -> ``nexus_orchestrator``
-> AgentRuntime -> typed AgentDecision -> kernel validation/execution.

There is no per-user agent row or model instance: the "personal" part is the caller's
server-resolved identity (organization, account, role and the membership's
student/faculty profile), which reaches the tools only through ``ToolContext``. Each
message becomes one mission owned by the caller, advanced by a small bounded number
of transitions.

Phase 2 capabilities are read-only and about the caller only. Future worker agents
(``FUTURE_DELEGATES``) are named in the orchestrator's delegation allowlist but are
NOT registered: the runtime shows the brain only registered agents and refuses
anything else, so until one is registered Nexus can only say the capability is
unavailable. Registering e.g. ``exam_guardian`` later makes it delegable with no
change here -- unless the agent refuses delegation (``accepts_delegation=False``), as the
Phase 3 ``assignment_guardian`` does: its missions are created only by publishing an
assignment. Nexus reads assignments through ``get_my_assignments`` / ``get_assignment_status``
(caller-scoped, read-only); creating one stays the faculty API's controlled write. Phase 4 adds the same for
exams (``get_my_exams``) and class attendance (``get_my_attendance_summary``); the Exam and Attendance
Guardians also refuse delegation (created only by exam scheduling / absence detection).
"""
from __future__ import annotations

import threading
from contextlib import nullcontext
from datetime import datetime
from typing import Any, ClassVar, Dict, FrozenSet, List, Literal, Optional, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agentos.registry import (
    AgentRegistry, AgentSpec, AgentTool, ToolContext, ToolExecutionError, ToolRegistry, ToolResult,
)
from app.agentos.runtime import SUBJECT, AgentOSError, AgentRuntime, MissionActor
from app.agentos.schemas import USER_MESSAGE_MAX, AgentContext, AgentDecision, CreateAgentMission, DecisionKind
from app.db.models.agent_kernel import TERMINAL_MISSION_STATUSES, AgentMission, AgentMissionStatus
from app.db.models.academic import AttendanceRecord, Course, Enrollment, Exam
from app.db.models.assignment import Assignment
from app.db.models.attendance_intervention import AttendanceIntervention, InterventionStatus
from app.db.models.auth import AuthAccount
from app.db.models.faculty import AttendanceSession, AttendanceSessionStatus, FacultyProfile, TeachingAssignment
from app.db.models.identity import Department, Student
from app.db.models.organization import Organization
from app.db.repositories import operations_audit
from app.llm.router import ai_context
from app.schemas.enums import UserRole
from app.services import assignments as assignment_service
from app.services import exams as exam_service

NEXUS_AGENT_KEY = "nexus_orchestrator"
NEXUS_ROLES: FrozenSet[UserRole] = frozenset({UserRole.STUDENT, UserRole.FACULTY, UserRole.HOD, UserRole.ADMIN})
# Planned worker agents. Not registered in Phase 2 -- never register a placeholder to make delegation "work".
FUTURE_DELEGATES: FrozenSet[str] = frozenset({
    "assignment_guardian", "exam_guardian", "attendance_guardian", "academic_agent", "communication_agent",
    "knowledge_agent", "sync_agent",
})
NEXUS_MAX_STEPS = 8
MAX_ASSISTANT_TRANSITIONS = 5
DEFAULT_ASSISTANT_TRANSITIONS = 4
SUCCESS_CRITERIA = ["Reply to the user with a brief user_message based only on tool results, "
                    "or say that the capability is not available yet."]
PROVIDER_UNAVAILABLE_STATUS = {"AI_BUDGET_EXCEEDED": 402}
# Phase 6: the stored goal of a request whose text must not be persisted (a voice transcript).
UNSTORED_GOAL = "[voice request - transcript not stored]"


# --- Read-only tools (caller-scoped; no input can name another person) ---------------------------------------------


class NoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActiveMissionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=5, ge=1, le=10)


def _department(session: Session, department_id: Optional[int]) -> Optional[Dict[str, Any]]:
    department = session.get(Department, department_id) if department_id is not None else None
    return {"code": department.code, "name": department.name} if department is not None else None


class GetMyIdentityContext(AgentTool):
    name: ClassVar[str] = "get_my_identity_context"
    description: ClassVar[str] = ("The signed-in user's own role, name, organization and student/faculty profile "
                                  "summary. Takes no input.")
    input_model = NoInput
    required_roles = NEXUS_ROLES

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        session = context.session
        account = session.get(AuthAccount, context.account_id)
        organization = session.get(Organization, context.organization_id)
        data: Dict[str, Any] = {
            "role": context.role.value,
            "display_name": account.display_name if account is not None else None,
            "organization": organization.name if organization is not None else None,
        }
        if context.role == UserRole.STUDENT and context.student_code:
            student = session.execute(select(Student).where(Student.student_code == context.student_code)).scalars().first()
            if student is not None:
                data["student"] = {"student_code": student.student_code, "department": _department(session, student.department_id),
                                   "year": student.year, "semester": student.semester, "section": student.section}
        elif context.role in (UserRole.FACULTY, UserRole.HOD) and context.faculty_profile_id is not None:
            faculty = session.get(FacultyProfile, context.faculty_profile_id)
            if faculty is not None:
                department = session.get(Department, faculty.department_id)
                data["faculty"] = {"designation": faculty.designation, "department": _department(session, faculty.department_id),
                                   "is_department_head": department is not None and department.hod_faculty_id == faculty.id}
        return ToolResult(ok=True, data=data)


class GetMyActiveMissions(AgentTool):
    name: ClassVar[str] = "get_my_active_missions"
    description: ClassVar[str] = "The signed-in user's own unfinished AgentOS missions (newest first)."
    input_model = ActiveMissionsInput
    required_roles = NEXUS_ROLES

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        limit = cast(ActiveMissionsInput, args).limit
        missions = context.session.execute(
            select(AgentMission).where(
                AgentMission.organization_id == context.organization_id,
                AgentMission.owner_account_id == context.account_id,
                AgentMission.id != context.mission_id,
                AgentMission.status.not_in(list(TERMINAL_MISSION_STATUSES)),
            ).order_by(AgentMission.id.desc()).limit(limit)
        ).scalars()
        return ToolResult(ok=True, data={"missions": [
            {"mission_id": m.id, "agent_key": m.agent_key, "status": m.status.value, "goal": (m.goal or "")[:120],
             "created_at": m.created_at.isoformat() if m.created_at else None} for m in missions]})


class AssignmentStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignment_id: int = Field(ge=1)


class GetMyAssignments(AgentTool):
    name: ClassVar[str] = "get_my_assignments"
    description: ClassVar[str] = ("Student: my assignments with deadline and my submission status. Faculty/HOD/admin: "
                                  "assignments I manage with submission counts. Takes no input.")
    input_model = NoInput
    required_roles = NEXUS_ROLES

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        session = context.session
        if context.role == UserRole.STUDENT:
            try:
                rows = assignment_service.my_assignments(session, context)
            except assignment_service.AssignmentError as exc:
                raise ToolExecutionError(exc.code) from None
            return ToolResult(ok=True, data={"assignments": [
                {"assignment_id": r.id, "title": r.title[:120], "course": r.course_code, "status": r.status.value,
                 "deadline_at": r.deadline_at.isoformat(),
                 "my_submission": r.submission_status.value if r.submission_status else None} for r in rows[:10]]})
        query = select(Assignment).order_by(Assignment.id.desc()).limit(10)
        if context.role != UserRole.ADMIN:
            query = query.where(Assignment.created_by_account_id == context.account_id)
        out = []
        for a in session.execute(query).scalars():
            progress = assignment_service.compute_progress(session, a)
            out.append({"assignment_id": a.id, "title": a.title[:120], "status": a.status.value,
                        "deadline_at": a.deadline_at.isoformat(), "target_count": progress.target_count,
                        "submitted_count": progress.submitted_count, "pending_count": len(progress.pending_ids)})
        return ToolResult(ok=True, data={"assignments": out})


class GetAssignmentStatus(AgentTool):
    name: ClassVar[str] = "get_assignment_status"
    description: ClassVar[str] = ("One assignment's status: for a student, their own submission; for staff who manage "
                                  "it, submission counts. Anything else is not found.")
    input_model = AssignmentStatusInput
    required_roles = NEXUS_ROLES

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        session, assignment_id = context.session, cast(AssignmentStatusInput, args).assignment_id
        try:
            if context.role == UserRole.STUDENT:
                mine = next((r for r in assignment_service.my_assignments(session, context) if r.id == assignment_id), None)
                if mine is None:
                    raise ToolExecutionError("ASSIGNMENT_NOT_FOUND")
                return ToolResult(ok=True, data={
                    "assignment_id": mine.id, "status": mine.status.value, "deadline_at": mine.deadline_at.isoformat(),
                    "my_submission": mine.submission_status.value if mine.submission_status else None})
            assignment = assignment_service.manageable(session, context, assignment_id)
        except assignment_service.AssignmentError as exc:
            raise ToolExecutionError(exc.code) from None
        progress = assignment_service.compute_progress(session, assignment)
        return ToolResult(ok=True, data={
            "assignment_id": assignment.id, "status": assignment.status.value,
            "deadline_at": assignment.deadline_at.isoformat(), "target_count": progress.target_count,
            "submitted_count": progress.submitted_count, "late_count": progress.late_count,
            "pending_count": len(progress.pending_ids), "guardian_mission_id": assignment.guardian_mission_id})


def _managed_exams_query(session: Session, context: ToolContext):
    """Managed exams the caller may manage (admin: all; faculty/HOD: created by them or for their class; HOD: also
    their department's classes) -- the same scope as ``exams.manageable``."""
    query = (select(Exam).join(TeachingAssignment, TeachingAssignment.id == Exam.teaching_assignment_id)
             .order_by(Exam.scheduled_start.desc(), Exam.id.desc()).limit(10))
    if context.role == UserRole.ADMIN:
        return query
    scope = Exam.created_by_account_id == context.account_id
    if context.faculty_profile_id is not None:
        scope = scope | (TeachingAssignment.faculty_id == context.faculty_profile_id)
        department = (assignment_service.headed_department_id(session, context.faculty_profile_id)
                      if context.role == UserRole.HOD else None)
        if department is not None:
            scope = scope | (TeachingAssignment.department_id == department)
    return query.where(scope)


class GetMyExams(AgentTool):
    name: ClassVar[str] = "get_my_exams"
    description: ClassVar[str] = ("Student: my scheduled exams with time and my attendance status. Faculty/HOD/admin: "
                                  "exams I manage with attendance counts. Takes no input.")
    input_model = NoInput
    required_roles = NEXUS_ROLES

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        session = context.session
        if context.role == UserRole.STUDENT:
            try:
                rows = exam_service.my_exams(session, context)
            except exam_service.ExamError as exc:
                raise ToolExecutionError(exc.code) from None
            return ToolResult(ok=True, data={"exams": [
                {"exam_id": r.id, "title": (r.title or "")[:120], "course": r.course_code, "type": r.exam_type,
                 "status": r.status.value if r.status else None, "starts_at": r.scheduled_at.isoformat(),
                 "my_attendance": r.my_attendance.value if r.my_attendance else None} for r in rows[:10]]})
        out = []
        for exam in session.execute(_managed_exams_query(session, context)).scalars():
            progress = exam_service.compute_progress(session, exam)
            out.append({"exam_id": exam.id, "title": (exam.title or "")[:120],
                        "status": exam.status.value if exam.status else None,
                        "starts_at": exam.scheduled_start.isoformat(), **progress.counts()})
        return ToolResult(ok=True, data={"exams": out})


class GetMyAttendanceSummary(AgentTool):
    name: ClassVar[str] = "get_my_attendance_summary"
    description: ClassVar[str] = ("Student: my raw attendance counters per course (attended / conducted) and open absence "
                                  "follow-ups. Faculty/HOD: my classes with sessions held and open absence cases. Admin: "
                                  "absence cases by status. Takes no input.")
    input_model = NoInput
    required_roles = NEXUS_ROLES

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        session = context.session
        if context.role == UserRole.STUDENT:
            student = (session.execute(select(Student).where(Student.student_code == context.student_code)).scalars().first()
                       if context.student_code else None)
            if student is None:
                raise ToolExecutionError("NOT_A_STUDENT")
            rows = session.execute(
                select(Course.code, AttendanceRecord.classes_attended, AttendanceRecord.classes_conducted)
                .join(Enrollment, Enrollment.id == AttendanceRecord.enrollment_id)
                .join(Course, Course.id == Enrollment.course_id).where(Enrollment.student_id == student.id)
                .order_by(Course.code).limit(20)).all()
            open_cases = len(session.execute(select(AttendanceIntervention.id).where(
                AttendanceIntervention.student_id == student.id,
                AttendanceIntervention.status == InterventionStatus.OPEN)).all())
            return ToolResult(ok=True, data={"courses": [
                {"course": code, "classes_attended": attended, "classes_conducted": conducted}
                for code, attended, conducted in rows], "open_absence_cases": open_cases})
        if context.role == UserRole.ADMIN:
            statuses = list(session.execute(select(AttendanceIntervention.status)).scalars())
            return ToolResult(ok=True, data={"absence_cases": {s.value: statuses.count(s) for s in InterventionStatus}})
        if context.faculty_profile_id is None:
            raise ToolExecutionError("NOT_A_FACULTY_MEMBER")
        classes = []
        for teaching in session.execute(select(TeachingAssignment).where(
                TeachingAssignment.faculty_id == context.faculty_profile_id).order_by(TeachingAssignment.id).limit(20)).scalars():
            held = len(session.execute(select(AttendanceSession.id).where(
                AttendanceSession.teaching_assignment_id == teaching.id,
                AttendanceSession.status == AttendanceSessionStatus.CLOSED)).all())
            open_cases = len(session.execute(select(AttendanceIntervention.id).where(
                AttendanceIntervention.teaching_assignment_id == teaching.id,
                AttendanceIntervention.status == InterventionStatus.OPEN)).all())
            course = session.get(Course, teaching.course_id)
            classes.append({"teaching_assignment_id": teaching.id, "course": course.code if course else None,
                            "section": teaching.section, "sessions_held": held, "open_absence_cases": open_cases})
        return ToolResult(ok=True, data={"classes": classes})


NEXUS_TOOLS = (GetMyIdentityContext, GetMyActiveMissions, GetMyAssignments, GetAssignmentStatus, GetMyExams,
               GetMyAttendanceSummary)


# --- Phase 6.4: the legacy read-only domain specialists, reached through the existing SpecialistGateway ------------

CONSULT_SPECIALIST_TOOL = "consult_domain_specialist"
MAX_SPECIALIST_ANSWER_CHARS = 1200


class ConsultSpecialistInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Intent definitions live in the schema (both real brains send it). The keys are the student chat keys.
    specialist: Literal["academic", "placements", "events", "complaints"] = Field(description=(
        "academic: timetable, class schedule, attendance and its policy, exams and exam eligibility, courses, grades. "
        "placements: internships, jobs, placement opportunities, skill gaps, applications. "
        "events: campus events, workshops, clubs, competitions and whether they clash with my classes. "
        "complaints: hostel, fees, facilities, IT and other campus services, grievance cases and their policies. "
        "Requests to register, book, submit or file something: consult the matching specialist for the candidates "
        "and their checks only. Nothing is registered or submitted from this assistant; say that the action needs "
        "the approval workflow."))
    question: str = Field(min_length=1, max_length=1000, description="The student's question, in their own words.")


class ConsultDomainSpecialist(AgentTool):
    """Read-only bridge to the registered legacy specialists (Academic, Career, Events, Campus Services). It reuses
    ``SpecialistGateway`` exactly as student agent chat does -- the same deployment gate, the student from the caller's
    membership, a session bound to the caller's organization -- so the Action Agent stays unreachable and nothing here
    can write. Policy answers come from the specialists' own RAG evidence."""

    name: ClassVar[str] = CONSULT_SPECIALIST_TOOL
    # The brain sees this text (clipped to 200 chars) but not the input fields' descriptions, so it carries the route.
    description: ClassVar[str] = ("Read-only, cited answers. academic: my timetable, attendance, exams, courses, policy. "
                                  "placements: jobs, internships. events: campus events. complaints: hostel, fees, "
                                  "facilities. Never guess these.")
    input_model = ConsultSpecialistInput
    required_roles = frozenset({UserRole.STUDENT})  # the gateway is student-scoped; staff have their own scoped chats

    def __init__(self, gateway: Any) -> None:
        self._gateway = gateway

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        from app.llm.base import LLMProviderError, LLMTransientError
        from app.llm.router import AIBudgetExceededError
        from app.services.agent_deployments import DeploymentError, require_active

        request = cast(ConsultSpecialistInput, args)
        if not context.student_code:
            raise ToolExecutionError("NOT_A_STUDENT")
        try:
            require_active(context.session, request.specialist, context.role.value)
        except DeploymentError as exc:
            raise ToolExecutionError(exc.code) from None
        try:
            answer = self._gateway.consult(request.specialist, request.question, student_id=context.student_code,
                                           organization_id=context.organization_id)
        except AIBudgetExceededError:
            raise ToolExecutionError("AI_BUDGET_EXCEEDED") from None
        except LLMTransientError:
            raise ToolExecutionError("PROVIDER_UNAVAILABLE") from None
        except LLMProviderError:
            raise ToolExecutionError("PROVIDER_ERROR") from None
        return ToolResult(ok=True, data={
            "specialist": request.specialist, "agent": answer.agent, "verification_status": answer.verification_status,
            "answer": answer.answer[:MAX_SPECIALIST_ANSWER_CHARS],
            "sources": [{"document_id": e.document_id, "title": e.title, "source": e.source} for e in answer.evidence[:5]],
            "issues": [issue[:200] for issue in answer.issues[:5]]})


# --- CAMPUS AI: grounded answers from campus records (SQL facts + organization-scoped knowledge) ----------------------

GROUNDED_TOOL = "answer_from_campus_records"
MAX_HISTORY_MISSIONS = 3


class CampusRecordsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=1000, description="The user's question, in their own words.")


def fit_message(text: str, limit: int) -> str:
    """Whole lines that fit in ``limit`` characters (a reply is never cut mid-fact); citations are always kept."""
    if len(text) <= limit:
        return text
    body, marker, sources = text.partition("\n\nSources: ")
    if marker and len(marker + sources) < limit // 2:
        return fit_message(body, limit - len(marker + sources)) + marker + sources
    kept: List[str] = []
    for line in text.splitlines():
        if len("\n".join([*kept, line])) > limit - 4:
            break
        kept.append(line)
    return ("\n".join(kept) + "\n...") if kept else text[: limit - 3] + "..."


class AnswerFromCampusRecords(AgentTool):
    """Read-only, caller-scoped answers: the minimum SQL facts for the question, at most four knowledge chunks of
    the caller's organization, deterministic rules, then (optionally) model phrasing that is validated against them.
    Identity comes only from the tool context; the question can never name another person's records."""

    name: ClassVar[str] = GROUNDED_TOOL
    description: ClassVar[str] = ("Grounded answer from campus records: my timetable, attendance, exams, exam eligibility, "
                                  "assignments, placements, events, cases, my classes' submissions, campus policies.")
    input_model = CampusRecordsInput
    required_roles = NEXUS_ROLES

    def __init__(self, service: Any) -> None:
        self._service = service

    @staticmethod
    def _history(context: ToolContext) -> List[Any]:
        from app.schemas.grounded import ConversationTurn

        missions = context.session.execute(select(AgentMission).where(
            AgentMission.organization_id == context.organization_id, AgentMission.owner_account_id == context.account_id,
            AgentMission.agent_key == NEXUS_AGENT_KEY, AgentMission.id != context.mission_id,
            AgentMission.status == AgentMissionStatus.COMPLETED,
        ).order_by(AgentMission.id.desc()).limit(MAX_HISTORY_MISSIONS)).scalars()
        turns = [ConversationTurn(user=(m.goal or "")[:300],
                                  assistant=str((m.context or {}).get("assistant_message") or "")[:300])
                 for m in missions if m.goal and m.goal != UNSTORED_GOAL]
        return list(reversed(turns))

    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        from app.services.grounded_facts import GroundedIdentity

        from app.services.agent_deployments import DeploymentError, require_active
        from app.services.grounded_answers import INTENT_PRODUCT, classify

        question = cast(CampusRecordsInput, args).question
        intent = classify(question, context.role)
        if intent is None:
            raise ToolExecutionError("NOT_A_RECORDS_QUESTION")
        try:
            require_active(context.session, INTENT_PRODUCT[intent.key], context.role.value)
        except DeploymentError as exc:
            raise ToolExecutionError(exc.code) from None
        account = context.session.get(AuthAccount, context.account_id)
        identity = GroundedIdentity(
            organization_id=context.organization_id, account_id=context.account_id, role=context.role,
            display_name=account.display_name if account is not None else None,
            student_code=context.student_code, faculty_profile_id=context.faculty_profile_id)
        answer = self._service.answer(context.session, identity, question, self._history(context))
        if answer is None:
            raise ToolExecutionError("NOT_A_RECORDS_QUESTION")
        return ToolResult(ok=True, data={
            "intent": answer.intent, "found": answer.found, "answer": fit_message(answer.answer, USER_MESSAGE_MAX),
            "answered_by": answer.answered_by, "synthesis_status": answer.synthesis_status,
            "sources": [c.model_dump() for c in answer.citations], "fact_count": answer.fact_count,
            "context_tokens_estimate": answer.context_tokens_estimate})


class GroundedRoutingBrain:
    """Deterministic front door for Nexus: a recognised campus-records question goes straight to
    ``answer_from_campus_records`` and its validated answer is the reply -- no AI decision is needed for either step.
    Everything else (other agents, other questions, a tool error) is decided by the wrapped brain unchanged. The
    decisions are ordinary ``AgentDecision`` objects that the kernel validates and executes as usual."""

    ROUTE = "grounded_records"

    def __init__(self, inner: Any, classify: Any) -> None:
        self.inner, self._classify = inner, classify
        self._thread = threading.local()

    def __getattr__(self, item: str) -> Any:  # available/code/connectivity/... of the wrapped brain
        inner = self.__dict__.get("inner")
        if inner is None:
            raise AttributeError(item)
        return getattr(inner, item)

    def _mine(self) -> bool:
        return bool(getattr(self._thread, "deterministic", False))

    @property
    def provider_name(self) -> Optional[str]:
        return "campus_records" if self._mine() else getattr(self.inner, "provider_name", None)

    @property
    def model_name(self) -> Optional[str]:
        return None if self._mine() else getattr(self.inner, "model_name", None)

    @property
    def last_route(self) -> Optional[str]:
        return self.ROUTE if self._mine() else getattr(self.inner, "last_route", None)

    @property
    def is_live(self) -> bool:
        return bool(getattr(self.inner, "is_live", False))

    @property
    def audit_calls(self) -> bool:
        return bool(getattr(self.inner, "audit_calls", False))

    def decide(self, context: AgentContext) -> AgentDecision:
        self._thread.deterministic = False
        decision = self._route(context)
        if decision is not None:
            self._thread.deterministic = True
            return decision
        return self.inner.decide(context)

    def _route(self, context: AgentContext) -> Optional[AgentDecision]:
        if context.agent_key != NEXUS_AGENT_KEY or GROUNDED_TOOL not in {t.name for t in context.allowed_tools}:
            return None
        results = [o for o in context.observations if o.kind in ("tool_result", "tool_error")]
        grounded = next((o for o in reversed(results) if o.source == GROUNDED_TOOL), None)
        if grounded is None:
            try:
                role = UserRole(context.caller_role) if context.caller_role else None
            except ValueError:
                role = None
            if results or role is None or self._classify(context.goal, role) is None:
                return None
            return AgentDecision(kind=DecisionKind.TOOL, tool_name=GROUNDED_TOOL,
                                 tool_input={"question": context.goal[:1000]})
        data = ((grounded.data or {}).get("data") or {}) if grounded.kind == "tool_result" else {}
        answer = data.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            return None
        return AgentDecision(kind=DecisionKind.COMPLETE, user_message=answer[:USER_MESSAGE_MAX],
                             outcome=f"Answered from campus records ({data.get('intent')}, {data.get('answered_by')}).")


def nexus_spec(extra_tools: FrozenSet[str] = frozenset()) -> AgentSpec:
    return AgentSpec(
        NEXUS_AGENT_KEY, "Personal assistant and orchestrator for the signed-in user.",
        allowed_tools=frozenset(t.name for t in NEXUS_TOOLS) | extra_tools,
        allowed_delegate_agents=FUTURE_DELEGATES, supported_roles=NEXUS_ROLES,
    )


def register_nexus(agents: AgentRegistry, tools: ToolRegistry, specialists: Any = None, grounded: Any = None) -> None:
    """``specialists``: the app's ``SpecialistGateway``. Without one (e.g. the worker script) Nexus has no specialist
    tool and says the capability is unavailable -- never a placeholder. ``grounded``: the CAMPUS AI
    ``GroundedAnswerService`` (adds ``answer_from_campus_records`` for every Nexus role)."""
    for tool_class in NEXUS_TOOLS:
        tools.register(tool_class())
    extra: FrozenSet[str] = frozenset()
    if specialists is not None:
        tools.register(ConsultDomainSpecialist(specialists))
        extra = extra | {CONSULT_SPECIALIST_TOOL}
    if grounded is not None:
        tools.register(AnswerFromCampusRecords(grounded))
        extra = extra | {GROUNDED_TOOL}
    agents.register(nexus_spec(frozenset(extra)))


# --- The PersonalAssistant service ---------------------------------------------------------------------------------


class BrainInfo(BaseModel):
    provider: Optional[str]
    model: Optional[str]
    live: bool


class AssistantReply(BaseModel):
    mission_id: int
    status: AgentMissionStatus
    assistant_message: Optional[str] = None
    waiting_for: Optional[str] = None
    steps_performed: int
    error_code: Optional[str] = None
    brain: BrainInfo


class AssistantMissionSummary(BaseModel):
    mission_id: int
    status: AgentMissionStatus
    goal: str
    assistant_message: Optional[str]
    waiting_for: Optional[str]
    step_count: int
    created_at: datetime
    updated_at: datetime


class AssistantUnavailable(AgentOSError):
    """The brain could not answer. The mission (if one was created) is kept and resumable."""

    def __init__(self, code: str, reply: Optional[AssistantReply] = None) -> None:
        super().__init__(code, "The assistant's AI provider is unavailable right now. Nothing was executed.",
                         PROVIDER_UNAVAILABLE_STATUS.get(code, 503))
        self.reply = reply


def _brain_info(brain: Any) -> BrainInfo:
    return BrainInfo(provider=getattr(brain, "provider_name", None) or None, model=getattr(brain, "model_name", None),
                     live=bool(getattr(brain, "is_live", False)))


class PersonalAssistant:
    def __init__(self, runtime: AgentRuntime, *, max_transitions: int = DEFAULT_ASSISTANT_TRANSITIONS) -> None:
        if not 1 <= max_transitions <= MAX_ASSISTANT_TRANSITIONS:
            raise ValueError(f"max_transitions must be 1-{MAX_ASSISTANT_TRANSITIONS}")
        self.runtime, self.max_transitions = runtime, max_transitions

    def handle_message(self, session: Session, actor: MissionActor, message: str, *, recorder: Any = None,
                       store_message: bool = True) -> AssistantReply:
        """One message -> one persistent mission owned by ``actor`` -> at most ``max_transitions`` transitions.

        ``store_message=False`` (Phase 6 voice): the mission's goal is a fixed placeholder and the text reaches the
        brain only from memory while these transitions run (``AgentRuntime.transient_goal``)."""
        brain = self.runtime.brain
        if not getattr(brain, "available", True):
            raise AssistantUnavailable(getattr(brain, "code", "BRAIN_UNAVAILABLE"))  # refused before any mission exists
        mission = self.runtime.create_mission(session, actor, CreateAgentMission(
            agent_key=NEXUS_AGENT_KEY, goal=message if store_message else UNSTORED_GOAL,
            success_criteria=SUCCESS_CRITERIA, max_steps=NEXUS_MAX_STEPS,
            context={"channel": "assistant" if store_message else "assistant_voice"}))
        operations_audit.record(
            session, event_type="NEXUS_REQUEST_RECEIVED", actor_account_id=actor.account_id, actor_role=actor.role.value,
            subject_type=SUBJECT, subject_id=str(mission.id), at=self.runtime.clock(),
            message=f"Nexus request received as mission {mission.id}.",
            metadata={"agent_key": NEXUS_AGENT_KEY, "message_chars": len(message), "brain": _brain_info(brain).provider,
                      "input": "text" if store_message else "voice"},
        )
        session.commit()
        transient = nullcontext() if store_message else self.runtime.transient_goal(mission.id, message)
        with transient, ai_context(actor.organization_id, mission_id=f"agentos:{mission.id}", recorder=recorder,
                                   agent_key=NEXUS_AGENT_KEY):
            results = self.runtime.run_until_blocked(session, actor, mission.id, max_transitions=self.max_transitions)
        mission = self.runtime.get_mission(session, actor, mission.id)
        last = results[-1] if results else None
        reply = AssistantReply(
            mission_id=mission.id, status=mission.status, assistant_message=(mission.context or {}).get("assistant_message"),
            waiting_for=mission.waiting_for, steps_performed=sum(1 for r in results if r.step_number is not None),
            error_code=last.error_code if last else None, brain=_brain_info(brain),
        )
        if last is not None and not last.transitioned and last.error_code:
            raise AssistantUnavailable(last.error_code, reply)
        return reply

    def list_missions(self, session: Session, actor: MissionActor, *, limit: int = 20) -> List[AssistantMissionSummary]:
        """The caller's own Nexus missions, newest first."""
        self.runtime._check_session(session, actor)
        missions = session.execute(
            select(AgentMission).where(
                AgentMission.organization_id == actor.organization_id, AgentMission.owner_account_id == actor.account_id,
                AgentMission.agent_key == NEXUS_AGENT_KEY,
            ).order_by(AgentMission.id.desc()).limit(max(1, min(limit, 50)))
        ).scalars()
        return [AssistantMissionSummary(
            mission_id=m.id, status=m.status, goal=m.goal, assistant_message=(m.context or {}).get("assistant_message"),
            waiting_for=m.waiting_for, step_count=m.step_count, created_at=m.created_at, updated_at=m.updated_at,
        ) for m in missions]
