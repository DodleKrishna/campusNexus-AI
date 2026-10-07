"""/agentos/missions (JWT): inspection/testing API for the AgentOS V2 kernel (Phase 1), and
/agentos/assistant (Phase 2): the personal Nexus assistant. No UI.

The organization, account, role and student/faculty profile come only from the
verified token; the request session is already bound to that organization, and
request bodies forbid extra fields. A caller sees, runs and cancels only their own
missions (an admin may also read and cancel missions in their organization);
anything else is a 404.
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.agentos.nexus import AssistantMissionSummary, AssistantReply, AssistantUnavailable, PersonalAssistant
from app.agentos.runtime import AgentOSError, AgentRuntime, MissionActor
from app.agentos.schemas import AgentMissionView, AgentResult, AgentStepView, CreateAgentMission
from app.api.auth_deps import AuthenticatedUser, require_authenticated_user
from app.api.deps import get_session
from app.llm.router import ai_context

router = APIRouter(prefix="/agentos/missions", tags=["agentos"])
assistant_router = APIRouter(prefix="/agentos/assistant", tags=["agentos"])


def get_agent_runtime(request: Request) -> AgentRuntime:
    return request.app.state.agent_runtime


def _actor(user: AuthenticatedUser = Depends(require_authenticated_user)) -> MissionActor:
    return MissionActor(organization_id=user.organization_id, account_id=user.account_id, role=user.role,
                        student_code=user.student_id, faculty_profile_id=user.faculty_profile_id)


def _recorder(request: Request):
    return getattr(request.app.state.llm_provider, "recorder", None)


def _http(exc: AgentOSError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message})


@router.post("", response_model=AgentMissionView, status_code=201)
def create_mission(body: CreateAgentMission, actor: MissionActor = Depends(_actor), session: Session = Depends(get_session),
                   runtime: AgentRuntime = Depends(get_agent_runtime)) -> AgentMissionView:
    try:
        return AgentMissionView.model_validate(runtime.create_mission(session, actor, body), from_attributes=True)
    except AgentOSError as exc:
        raise _http(exc) from None


@router.get("/{mission_id}", response_model=AgentMissionView)
def get_mission(mission_id: int, actor: MissionActor = Depends(_actor), session: Session = Depends(get_session),
                runtime: AgentRuntime = Depends(get_agent_runtime)) -> AgentMissionView:
    try:
        return AgentMissionView.model_validate(runtime.get_mission(session, actor, mission_id), from_attributes=True)
    except AgentOSError as exc:
        raise _http(exc) from None


@router.get("/{mission_id}/steps", response_model=List[AgentStepView])
def mission_steps(mission_id: int, actor: MissionActor = Depends(_actor), session: Session = Depends(get_session),
                  runtime: AgentRuntime = Depends(get_agent_runtime)) -> List[AgentStepView]:
    try:
        return [AgentStepView.model_validate(s, from_attributes=True) for s in runtime.list_steps(session, actor, mission_id)]
    except AgentOSError as exc:
        raise _http(exc) from None


@router.post("/{mission_id}/run-step", response_model=AgentResult)
def run_step(mission_id: int, request: Request, actor: MissionActor = Depends(_actor),
             session: Session = Depends(get_session), runtime: AgentRuntime = Depends(get_agent_runtime)) -> AgentResult:
    try:
        with ai_context(actor.organization_id, mission_id=f"agentos:{mission_id}", recorder=_recorder(request)):
            return runtime.run_step(session, actor, mission_id)
    except AgentOSError as exc:
        raise _http(exc) from None


@router.post("/{mission_id}/cancel", response_model=AgentMissionView)
def cancel_mission(mission_id: int, actor: MissionActor = Depends(_actor), session: Session = Depends(get_session),
                   runtime: AgentRuntime = Depends(get_agent_runtime)) -> AgentMissionView:
    try:
        return AgentMissionView.model_validate(runtime.cancel(session, actor, mission_id), from_attributes=True)
    except AgentOSError as exc:
        raise _http(exc) from None


# --- Phase 2: the personal Nexus assistant ------------------------------------------------------------------------


class AssistantMessage(BaseModel):
    """Only the message. Organization, account, role and profile come from the token (extra fields: 422)."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=1000)


@assistant_router.post("/message", response_model=AssistantReply)
def assistant_message(body: AssistantMessage, request: Request, actor: MissionActor = Depends(_actor),
                      session: Session = Depends(get_session),
                      runtime: AgentRuntime = Depends(get_agent_runtime)) -> AssistantReply:
    try:
        return PersonalAssistant(runtime).handle_message(session, actor, body.message, recorder=_recorder(request))
    except AssistantUnavailable as exc:
        detail = {"code": exc.code, "message": exc.message}
        if exc.reply is not None:  # the mission is kept and can be resumed with /agentos/missions/{id}/run-step
            detail.update(mission_id=exc.reply.mission_id, status=exc.reply.status.value,
                          steps_performed=exc.reply.steps_performed)
        raise HTTPException(status_code=exc.status_code, detail=detail) from None
    except AgentOSError as exc:
        raise _http(exc) from None


@assistant_router.get("/missions", response_model=List[AssistantMissionSummary])
def assistant_missions(limit: int = Query(default=20, ge=1, le=50), actor: MissionActor = Depends(_actor),
                       session: Session = Depends(get_session),
                       runtime: AgentRuntime = Depends(get_agent_runtime)) -> List[AssistantMissionSummary]:
    try:
        return PersonalAssistant(runtime).list_missions(session, actor, limit=limit)
    except AgentOSError as exc:
        raise _http(exc) from None
