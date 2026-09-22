"""Human-in-the-loop approval schema enforced by the Approval Gate.

An ApprovalRequest always references the ToolCall it gates. Its decision
(approve/reject/edit) plus who/when is the audit record CLAUDE.md's Permission /
Approval Rules require -- a pending request must never silently resolve itself.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import NonBlankStr, utc_now
from app.schemas.enums import ApprovalStatus


class ApprovalRequest(BaseModel):
    """A pending or resolved human approval decision for a sensitive tool call."""

    approval_id: NonBlankStr
    mission_id: NonBlankStr
    task_id: NonBlankStr
    tool_call_id: NonBlankStr
    action_summary: NonBlankStr
    requested_by: NonBlankStr
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision_by: Optional[str] = None
    decision_at: Optional[datetime] = None
    decision_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _decision_fields_match_status(self) -> "ApprovalRequest":
        if self.status == ApprovalStatus.PENDING:
            if self.decision_by is not None or self.decision_at is not None:
                raise ValueError(
                    "a PENDING approval must not have decision_by/decision_at set"
                )
        elif not self.decision_by or self.decision_at is None:
            raise ValueError(
                "a resolved approval (APPROVED/REJECTED/EDIT_REQUIRED) must record "
                "decision_by and decision_at"
            )
        return self
