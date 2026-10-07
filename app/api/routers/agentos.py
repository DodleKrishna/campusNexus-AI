"""/agentos/missions (JWT): inspection/testing API for the AgentOS V2 kernel (Phase 1). No UI.

The organization, account and role come only from the verified token; the
request session is already bound to that organization. A caller sees, runs and
cancels only their own missions (an admin may also read and cancel missions in
their organization); anything else is a 404.
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.agentos.runtime import AgentOSError, AgentRuntime, MissionActor
from app.agentos.schemas import AgentMissionView, AgentResult, AgentStepView, CreateAgentMission
from app.api.auth_deps import AuthenticatedUser, require_authenticated_user
from app.api.deps import get_session

router = APIRouter(prefix="/agentos/missions", tags=["agentos"])


def get_agent_runtime(request: Request) -> AgentRuntime:
    return request.app.state.agent_runtime


def _actor(user: AuthenticatedUser = Depends(require_authenticated_user)) -> MissionActor:
    return MissionActor(organization_id=user.organization_id, account_id=user.account_id, role=user.role)


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
def run_step(mission_id: int, actor: MissionActor = Depends(_actor), session: Session = Depends(get_session),
             runtime: AgentRuntime = Depends(get_agent_runtime)) -> AgentResult:
    try:
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
