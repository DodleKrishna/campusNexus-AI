"""GET /agents, POST /agents/{agent_key}/query (Phase 15) -- student agent chat.

Students only; the student is taken from the JWT. Only the four read-only
specialists and the read-only Enquiry Agent are reachable, so chat can never
write anything. Provider problems become clean messages, never tracebacks.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from sqlalchemy.orm import Session

from app.api.ai import budget_exceeded, deployed_agent_run
from app.llm.router import AIBudgetExceededError
from app.agents.enquiry.agent import EnquiryAgent
from app.api.auth_deps import AuthenticatedUser, current_student, require_authenticated_user
from app.api.deps import get_now, get_session
from app.services.class_schedule import get_current_class
from app.llm.base import LLMProviderError, LLMTransientError
from app.schemas.agent_chat import (
    CHAT_AGENT_KEYS,
    DISPLAY_NAMES,
    SPECIALIST_AGENTS,
    AgentQueryRequest,
    AgentQueryResponse,
)
from app.services.agent_chat import SpecialistGateway

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agents", tags=["agents"])

_DESCRIPTIONS = {
    "academic": "Attendance, exam eligibility, timetable and exam schedule, grounded in academic policy.",
    "events": "Campus events and workshops, checked against your classes and exams.",
    "placements": "Internship eligibility, skill gaps and application status.",
    "complaints": "Your grievance cases, SLA status and the escalation procedure.",
    "enquiry": "General campus questions answered by consulting the other agents. Read-only.",
    "permission": "Prepares event permission, attendance permission, leave and OD requests and routes them to the right faculty.",
}


class AgentCatalogItem(BaseModel):
    key: str
    display_name: str
    description: str
    backend_agent: str | None
    available: bool


class AgentCatalog(BaseModel):
    live_ai: bool
    provider: str
    model: str | None
    agents: List[AgentCatalogItem]


def _gateway(request: Request) -> SpecialistGateway:
    return request.app.state.specialist_gateway


def _live_class(request: Request, organization_id: int, student_id: str, now: datetime):
    """Deterministic live class status (read-only) for the Enquiry Agent, in the caller's organization."""
    session = request.app.state.session_factory.open_tenant_session(organization_id)
    try:
        return get_current_class(session, student_id, now)
    finally:
        session.close()


@router.get("", response_model=AgentCatalog)
def catalog(request: Request, _: str = Depends(current_student)) -> AgentCatalog:
    provider = request.app.state.llm_provider
    items = [
        AgentCatalogItem(
            key=key, display_name=DISPLAY_NAMES[key], description=_DESCRIPTIONS[key],
            backend_agent=SPECIALIST_AGENTS[key].value if key in SPECIALIST_AGENTS else None, available=True,
        )
        for key in CHAT_AGENT_KEYS
    ]
    # Phase 16: the Permission Agent works through /requests/prepare (preview, then Confirm & Send).
    items.append(AgentCatalogItem(
        key="permission", display_name="Permission Agent", description=_DESCRIPTIONS["permission"],
        backend_agent=None, available=True,
    ))
    return AgentCatalog(live_ai=bool(provider.is_live), provider=provider.name, model=provider.model_name, agents=items)


@router.post("/{agent_key}/query", response_model=AgentQueryResponse)
def query(
    agent_key: str,
    body: AgentQueryRequest,
    request: Request,
    student_id: str = Depends(current_student),
    user: AuthenticatedUser = Depends(require_authenticated_user),  # cached: the same resolved identity
    gateway: SpecialistGateway = Depends(_gateway),
    now: datetime = Depends(get_now),
    session: Session = Depends(get_session),
) -> AgentQueryResponse:
    organization_id = user.organization_id
    if agent_key not in CHAT_AGENT_KEYS:
        raise HTTPException(status_code=404, detail=f"There is no '{agent_key}' agent available to chat with.")
    provider = request.app.state.llm_provider
    try:
        with deployed_agent_run(request, session, agent_key, "student", organization_id):
            if agent_key == "enquiry":
                agent = EnquiryAgent(
                    llm_provider=provider,
                    consult=lambda key, objective: gateway.consult(key, objective, student_id=student_id,
                                                                   organization_id=organization_id),
                    live_class=lambda at: _live_class(request, organization_id, student_id, at),
                )
                return agent.handle(body.message, now=now)
            answer = gateway.consult(agent_key, body.message, student_id=student_id, organization_id=organization_id)
    except AIBudgetExceededError as exc:
        raise budget_exceeded(exc) from exc
    except LLMTransientError as exc:
        logger.warning("agent chat: provider unavailable (%s)", exc.details())
        raise HTTPException(status_code=503, detail="Live AI is temporarily unavailable. Please try again shortly.") from exc
    except LLMProviderError as exc:
        logger.error("agent chat: provider error: %s", exc)
        raise HTTPException(status_code=502, detail="The AI provider returned an unusable response. Please try again.") from exc
    return AgentQueryResponse(
        agent_key=agent_key, display_name=DISPLAY_NAMES[agent_key], verification_status=answer.verification_status,
        answer=answer.answer, facts=answer.facts, evidence=answer.evidence, issues=answer.issues,
        live_ai=bool(provider.is_live),
    )
