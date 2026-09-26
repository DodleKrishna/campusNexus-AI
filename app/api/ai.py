"""API helpers for routed AI calls: organization attribution and the explicit budget stop."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

from fastapi import HTTPException, Request

from app.llm.router import AI_BUDGET_EXCEEDED, AIBudgetExceededError, AIContext, ai_context


@contextmanager
def request_ai_context(request: Request, organization_id: Optional[int]) -> Iterator[AIContext]:
    """AI calls inside the block are attributed to the caller's organization (from its server-side identity)."""
    with ai_context(organization_id, recorder=getattr(request.app.state.llm_provider, "recorder", None)) as context:
        yield context


def budget_exceeded(exc: AIBudgetExceededError) -> HTTPException:
    """402 with a machine-readable code: AI-required work stops; nothing is fabricated or downgraded."""
    return HTTPException(status_code=402, detail={"code": AI_BUDGET_EXCEEDED, "message": str(exc)})


@contextmanager
def deployed_agent_run(request: Request, session, chat_key: str, role: str, organization_id: Optional[int]) -> Iterator[AIContext]:
    """Agent-as-a-Product gate + attribution: the organization's deployment must exist, be active and allow the
    role (else 403 AGENT_NOT_DEPLOYED / AGENT_DISABLED / AGENT_ROLE_NOT_ALLOWED); its configured intelligence
    level then drives the router for this run."""
    from app.schemas.enums import IntelligenceLevel
    from app.services.agent_deployments import DeploymentError, require_active

    try:
        deployment = require_active(session, chat_key, role)
    except DeploymentError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
    override = deployment.intelligence_level if deployment is not None else None
    with ai_context(organization_id, recorder=getattr(request.app.state.llm_provider, "recorder", None),
                    agent_key=deployment.agent_key if deployment is not None else None,
                    level_override=override if override != IntelligenceLevel.NO_AI else None) as context:
        yield context
