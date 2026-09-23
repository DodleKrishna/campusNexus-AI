"""The Tool Gateway (restricted write-tool registry, CLAUDE.md Tool Security Rules).

Centralizes every check that must happen *in the gateway*, not only in an
LLM prompt: unknown-tool rejection, role authorization, ownership (the
caller's own identity -- never a client/arguments-supplied claim -- must
match the resource the arguments target), Pydantic validation of the raw
arguments dict against the tool's declared input schema (a handler never
runs on an unvalidated dict), and an idempotency short-circuit against
already-persisted ``ToolCallRecord`` rows so a retried call never
re-executes a write that already succeeded.

Only the Action Agent ever holds/calls a ``ToolGateway`` -- no other
specialist agent's factory (app/graph/registry.py) constructs one, which is
what "only the Action Agent may invoke side-effecting tools" means
structurally, not just by convention.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, Type

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.db.repositories.missions import get_tool_call_by_idempotency_key
from app.schemas.enums import ToolExecutionStatus, UserRole
from app.schemas.tools import ToolCall, ToolResult

ToolHandler = Callable[[Session, BaseModel, str], ToolResult]
"""``(session, validated_input, tool_call_id) -> ToolResult``."""


@dataclass(frozen=True)
class ToolDefinition:
    """A single write tool's full declared contract."""

    name: str
    input_model: Type[BaseModel]
    authorized_roles: FrozenSet[UserRole]
    required_permission: str
    requires_approval: bool
    idempotency_strategy: str
    postconditions: str
    handler: ToolHandler


class UnknownToolError(Exception):
    """Raised by ``ToolGateway.get`` for an unregistered tool name."""


class ToolGateway:
    """The restricted registry + sole enforcement point for every write tool."""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        self._tools[definition.name] = definition

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise UnknownToolError(
                f"No tool registered for {name!r}; supported: {sorted(self._tools)}"
            ) from exc

    def is_registered(self, name: str) -> bool:
        return name in self._tools

    def execute(
        self,
        session: Session,
        tool_call: ToolCall,
        *,
        caller_role: UserRole,
        caller_student_id: str,
    ) -> ToolResult:
        """Validate + authorize + run one WRITE ``ToolCall``, never raising.

        Every failure mode (unknown tool, unauthorized role, ownership
        mismatch, invalid arguments, a handler-raised exception) becomes a
        FAILED ``ToolResult`` with a traceable ``error`` -- never an
        unhandled exception and never a silent no-op.
        """
        try:
            definition = self.get(tool_call.tool_name)
        except UnknownToolError as exc:
            return ToolResult(tool_call_id=tool_call.tool_call_id, status=ToolExecutionStatus.FAILED, error=str(exc))

        if caller_role not in definition.authorized_roles:
            return ToolResult(
                tool_call_id=tool_call.tool_call_id,
                status=ToolExecutionStatus.FAILED,
                error=f"role {caller_role.value!r} is not authorized to call {definition.name!r}",
            )

        argument_student_id = tool_call.arguments.get("student_id")
        if argument_student_id != caller_student_id:
            return ToolResult(
                tool_call_id=tool_call.tool_call_id,
                status=ToolExecutionStatus.FAILED,
                error=(
                    f"ownership check failed: arguments target student_id={argument_student_id!r}, "
                    f"caller is student_id={caller_student_id!r}"
                ),
            )

        try:
            validated = definition.input_model.model_validate(tool_call.arguments)
        except ValidationError as exc:
            return ToolResult(
                tool_call_id=tool_call.tool_call_id, status=ToolExecutionStatus.FAILED, error=f"invalid arguments: {exc}"
            )

        # Idempotency short-circuit: a prior SUCCESS with this exact key is
        # replayed, never re-executed (CLAUDE.md Idempotency Requirement).
        idempotency_key = tool_call.idempotency_key
        assert idempotency_key, "WRITE ToolCall.idempotency_key is required (enforced by the schema itself)"
        existing_record = get_tool_call_by_idempotency_key(session, idempotency_key)
        if existing_record is not None and existing_record.status == ToolExecutionStatus.SUCCESS:
            return ToolResult(
                tool_call_id=tool_call.tool_call_id,
                status=ToolExecutionStatus.SUCCESS,
                data=existing_record.result_data,
                executed_at=existing_record.executed_at,
                postcondition_verified=existing_record.postcondition_verified,
            )

        try:
            return definition.handler(session, validated, tool_call.tool_call_id)
        except Exception as exc:  # noqa: BLE001 -- a handler-internal failure becomes a FAILED result, never a crash
            session.rollback()
            return ToolResult(
                tool_call_id=tool_call.tool_call_id,
                status=ToolExecutionStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
