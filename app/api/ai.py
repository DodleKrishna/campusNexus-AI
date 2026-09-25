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
