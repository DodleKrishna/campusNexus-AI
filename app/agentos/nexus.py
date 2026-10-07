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
change here.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agentos.registry import AgentRegistry, AgentSpec, AgentTool, ToolContext, ToolRegistry, ToolResult
from app.agentos.runtime import SUBJECT, AgentOSError, AgentRuntime, MissionActor
from app.agentos.schemas import CreateAgentMission
from app.db.models.agent_kernel import TERMINAL_MISSION_STATUSES, AgentMission, AgentMissionStatus
from app.db.models.auth import AuthAccount
from app.db.models.faculty import FacultyProfile
from app.db.models.identity import Department, Student
from app.db.models.organization import Organization
from app.db.repositories import operations_audit
from app.llm.router import ai_context
from app.schemas.enums import UserRole

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


def nexus_spec() -> AgentSpec:
    return AgentSpec(
        NEXUS_AGENT_KEY, "Personal assistant and orchestrator for the signed-in user.",
        allowed_tools=frozenset({GetMyIdentityContext.name, GetMyActiveMissions.name}),
        allowed_delegate_agents=FUTURE_DELEGATES, supported_roles=NEXUS_ROLES,
    )


def register_nexus(agents: AgentRegistry, tools: ToolRegistry) -> None:
    tools.register(GetMyIdentityContext())
    tools.register(GetMyActiveMissions())
    agents.register(nexus_spec())


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

    def handle_message(self, session: Session, actor: MissionActor, message: str, *, recorder: Any = None) -> AssistantReply:
        """One message -> one persistent mission owned by ``actor`` -> at most ``max_transitions`` transitions."""
        brain = self.runtime.brain
        if not getattr(brain, "available", True):
            raise AssistantUnavailable(getattr(brain, "code", "BRAIN_UNAVAILABLE"))  # refused before any mission exists
        mission = self.runtime.create_mission(session, actor, CreateAgentMission(
            agent_key=NEXUS_AGENT_KEY, goal=message, success_criteria=SUCCESS_CRITERIA, max_steps=NEXUS_MAX_STEPS,
            context={"channel": "assistant"}))
        operations_audit.record(
            session, event_type="NEXUS_REQUEST_RECEIVED", actor_account_id=actor.account_id, actor_role=actor.role.value,
            subject_type=SUBJECT, subject_id=str(mission.id), at=self.runtime.clock(),
            message=f"Nexus request received as mission {mission.id}.",
            metadata={"agent_key": NEXUS_AGENT_KEY, "message_chars": len(message), "brain": _brain_info(brain).provider},
        )
        session.commit()
        with ai_context(actor.organization_id, mission_id=f"agentos:{mission.id}", recorder=recorder,
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
