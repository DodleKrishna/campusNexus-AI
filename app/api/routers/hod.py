"""/hod/* (Phase 17): department operations for the head of department. Read-only.

Every endpoint depends on ``current_hod``: HOD role AND a linked faculty
profile AND that profile recorded as the department's head. The department is
derived from that, never accepted from the client, so no parameter here can
widen the scope to another department.

Deciding requests stays on ``/requests/{id}/approve|reject`` (the routed
reviewer only); the HOD's inbox is ``GET /requests``.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.enquiry.hod import HOD_AGENT_KEYS, HOD_DISPLAY_NAMES, HodAgent
from app.api.auth_deps import current_hod
from app.api.deps import get_knowledge_service, get_now, get_session
from app.llm.base import LLMProviderError, LLMTransientError
from app.schemas.agent_chat import AgentQueryRequest, AgentQueryResponse
from app.schemas.department import (
    AttendanceInsights,
    DepartmentClassView,
    DepartmentComplaint,
    DepartmentDashboard,
    DepartmentOverview,
    FacultySummary,
    HodProfileView,
    StudentRisk,
)
from app.services import department_ops
from app.services.department_ops import HodScope
from app.services.knowledge import KnowledgeService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/hod", tags=["hod"])

_AGENT_DESCRIPTIONS = {
    "academic": "Department classes running, delayed or not held, and attendance below the requirement.",
    "enquiry": "Broad department questions answered from every department area. Read-only.",
    "permission": "Faculty requests and escalated student requests waiting for you.",
    "complaints": "Complaints from your department's students and their SLA state.",
    "events": "Upcoming campus events and how many of your students registered.",
}


class HodAgentItem(BaseModel):
    key: str
    display_name: str
    description: str


class HodAgentCatalog(BaseModel):
    live_ai: bool
    provider: str
    model: Optional[str]
    agents: List[HodAgentItem]


@router.get("/me", response_model=HodProfileView)
def me(scope: HodScope = Depends(current_hod)) -> HodProfileView:
    return department_ops.profile(scope)


@router.get("/dashboard", response_model=DepartmentDashboard)
def dashboard(scope: HodScope = Depends(current_hod), session: Session = Depends(get_session), now: datetime = Depends(get_now)) -> DepartmentDashboard:
    return department_ops.dashboard(session, scope, now)


@router.get("/department", response_model=DepartmentOverview)
def department(
    scope: HodScope = Depends(current_hod), session: Session = Depends(get_session),
    knowledge: KnowledgeService = Depends(get_knowledge_service), now: datetime = Depends(get_now),
) -> DepartmentOverview:
    return department_ops.overview(session, knowledge, scope, now)


@router.get("/activity", response_model=List[DepartmentClassView])
def activity(scope: HodScope = Depends(current_hod), session: Session = Depends(get_session), now: datetime = Depends(get_now)) -> List[DepartmentClassView]:
    return department_ops.activity(session, scope, now)


@router.get("/faculty", response_model=List[FacultySummary])
def faculty(scope: HodScope = Depends(current_hod), session: Session = Depends(get_session), now: datetime = Depends(get_now)) -> List[FacultySummary]:
    return department_ops.faculty_list(session, scope, now)


@router.get("/students", response_model=List[StudentRisk])
def students(
    scope: HodScope = Depends(current_hod), session: Session = Depends(get_session),
    knowledge: KnowledgeService = Depends(get_knowledge_service), now: datetime = Depends(get_now),
) -> List[StudentRisk]:
    return department_ops.student_risk(session, knowledge, scope, now)


@router.get("/attendance", response_model=AttendanceInsights)
def attendance(
    scope: HodScope = Depends(current_hod), session: Session = Depends(get_session),
    knowledge: KnowledgeService = Depends(get_knowledge_service), now: datetime = Depends(get_now),
) -> AttendanceInsights:
    return department_ops.attendance_insights(session, knowledge, scope, now)


@router.get("/complaints", response_model=List[DepartmentComplaint])
def complaints(scope: HodScope = Depends(current_hod), session: Session = Depends(get_session), now: datetime = Depends(get_now)) -> List[DepartmentComplaint]:
    return department_ops.complaints(session, scope, now)


@router.get("/agents", response_model=HodAgentCatalog)
def agent_catalog(request: Request, _: HodScope = Depends(current_hod)) -> HodAgentCatalog:
    provider = request.app.state.llm_provider
    return HodAgentCatalog(
        live_ai=bool(provider.is_live), provider=provider.name, model=provider.model_name,
        agents=[HodAgentItem(key=k, display_name=HOD_DISPLAY_NAMES[k], description=_AGENT_DESCRIPTIONS[k]) for k in HOD_AGENT_KEYS],
    )


@router.post("/agents/{agent_key}/query", response_model=AgentQueryResponse)
def agent_query(
    agent_key: str, body: AgentQueryRequest, request: Request, scope: HodScope = Depends(current_hod),
    session: Session = Depends(get_session), knowledge: KnowledgeService = Depends(get_knowledge_service),
    now: datetime = Depends(get_now),
) -> AgentQueryResponse:
    if agent_key not in HOD_AGENT_KEYS:
        raise HTTPException(status_code=404, detail=f"There is no '{agent_key}' agent available to chat with.")
    agent = HodAgent(llm_provider=request.app.state.llm_provider, knowledge=knowledge)
    try:
        return agent.handle(session, scope, agent_key, body.message, now)
    except LLMTransientError as exc:
        logger.warning("hod chat: provider unavailable (%s)", exc.details())
        raise HTTPException(status_code=503, detail="Live AI is temporarily unavailable. Please try again shortly.") from exc
    except LLMProviderError as exc:
        logger.error("hod chat: provider error: %s", exc)
        raise HTTPException(status_code=502, detail="The AI provider returned an unusable response. Please try again.") from exc
