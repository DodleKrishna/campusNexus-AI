"""The Permission Agent (Phase 16) -- prepares and routes a student's request. It never grants one.

    message -> interpret (LLM, structured ``PermissionIntent``)
    -> collect context (deterministic: event, date, affected classes, attendance)
    -> route (deterministic: ``app/rules/request_routing.py``)
    -> persist a DRAFT the student must confirm before it is sent

Phase 17: the same agent serves faculty members. Their requests (leave,
class substitution, OD, department permission) collect the faculty member's
own affected classes and are routed to the department HOD.

The LLM only interprets the requester's words. It cannot name a reviewer,
pick an event the words do not name, compute a date or change a status.
Sending (DRAFT -> PENDING) is the student's explicit confirmation through
the API, and the decision belongs to the routed faculty member alone.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.llm.base import LLMProvider
from app.schemas.workflow import PermissionIntent, PermissionPreview
from app.services import workflow_requests as requests_service


class PermissionAgent:
    def __init__(self, *, llm_provider: LLMProvider) -> None:
        self._llm = llm_provider

    def prepare(
        self, session: Session, *, requester: requests_service.Requester, message: str, now: datetime,
        required_percentage: Optional[float],
    ) -> PermissionPreview:
        live = bool(getattr(self._llm, "is_live", False))
        intent: PermissionIntent = self._llm.plan_permission_request(message)
        interpretation = intent.model_dump(mode="json")
        collected = requests_service.collect_context(session, requester, intent, message, now, required_percentage)
        if isinstance(collected, requests_service.NeedsInput):
            outcome = "not_supported" if intent.request_type == "unclear" else "needs_clarification"
            return PermissionPreview(
                outcome=outcome, message=collected.message, options=collected.options,
                interpretation=interpretation, live_ai=live,
            )
        routing = requests_service.route(session, requester, collected)
        reason = (intent.reason or message).strip()
        draft = requests_service.create_draft(session, requester.account, requester, collected, routing, reason, now)
        if routing.resolved:
            summary = f"I prepared your {collected.title}. {routing.note} Review it and choose Confirm & Send."
        else:
            summary = (
                f"I prepared your {collected.title}, but {routing.note[0].lower()}{routing.note[1:]} "
                "If you send it, it will wait for review."
            )
        return PermissionPreview(
            outcome="draft_ready", message=summary, request=requests_service.view(session, draft),
            interpretation=interpretation, live_ai=live,
        )
