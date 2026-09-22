"""Tool invocation and result schemas.

Any agent may issue a READ ToolCall (retrieval/lookup tools). Only the Action
Agent may issue and execute a WRITE ToolCall, and every WRITE call must carry an
idempotency key so a retried mission step never double-submits/books/charges
(CLAUDE.md Tool Security Rules, Idempotency Requirement).
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import JsonValue, NonBlankStr, utc_now
from app.schemas.enums import ToolAccessType, ToolExecutionStatus, UserRole


class ToolCall(BaseModel):
    """A request to invoke a specific, allowlisted tool with validated arguments."""

    tool_call_id: NonBlankStr
    mission_id: NonBlankStr
    task_id: NonBlankStr
    tool_name: NonBlankStr
    arguments: Dict[str, JsonValue] = Field(default_factory=dict)
    read_or_write: ToolAccessType
    required_role: Optional[UserRole] = None
    requires_approval: bool = False
    idempotency_key: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _write_calls_require_idempotency_key(self) -> "ToolCall":
        if self.read_or_write == ToolAccessType.WRITE and not (
            self.idempotency_key and self.idempotency_key.strip()
        ):
            raise ValueError("a WRITE ToolCall must have a non-blank idempotency_key")
        return self


class ToolResult(BaseModel):
    """The outcome of executing a ToolCall, including post-condition verification."""

    tool_call_id: NonBlankStr
    status: ToolExecutionStatus
    data: Optional[Dict[str, JsonValue]] = None
    error: Optional[str] = None
    executed_at: Optional[datetime] = None
    postcondition_verified: Optional[bool] = None
