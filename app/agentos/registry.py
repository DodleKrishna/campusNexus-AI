"""AgentRegistry and ToolRegistry: the only things a decision can name.

Agents and tools are registered by code at startup, never by a model. A decision
names a tool or agent by identifier; the runtime looks it up here and in the
calling agent's allowlist, so a model can never reach an import path, URL,
shell command, SQL statement or unregistered tool.

Phase 1 registers read-only tools only: a side-effecting tool is refused, so
writes keep going through the Verifier -> Approval Gate -> Action Agent path
until a later phase wires that path into the kernel.

Phase 3 adds *internal request* tools (``request_models``): code that may only stage
rows of the declared models in the caller's tenant session (e.g. a follow-up request
plus its domain event and audit row) after a deterministic policy allowed it. Nothing
leaves the system; the runtime verifies every write the tool made against that list.
It also lets an ``AgentSpec`` narrow the decisions and wait events its brain may use,
refuse delegation and user-created missions, opt in to the due-mission worker, and
carry a deterministic ``MissionSupervisor`` (``app.agentos.supervisor``).
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional, Type

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.agentos.schemas import IDENTIFIER, TOOL_NAME, DecisionKind, DomainEventType, ToolDescriptor
from app.db.base import Base
from app.schemas.enums import UserRole


class RegistryError(ValueError):
    """A registration violates the kernel's safety rules."""


@dataclass(frozen=True)
class AgentSpec:
    key: str
    description: str
    allowed_tools: FrozenSet[str] = frozenset()
    allowed_delegate_agents: FrozenSet[str] = frozenset()
    supported_roles: FrozenSet[UserRole] = frozenset()
    allowed_decisions: FrozenSet[DecisionKind] = frozenset()  # empty: every decision kind
    wait_events: FrozenSet[DomainEventType] = frozenset()  # empty: any allowlisted event
    accepts_delegation: bool = True  # False: no other agent may delegate to this one
    user_creatable: bool = True  # False: missions are created only by an application service
    autonomous: bool = False  # True: advanced by the due-mission worker (``app.agentos.worker``)
    supervisor: Optional[Any] = None  # a ``MissionSupervisor``: deterministic checks around the brain


@dataclass(frozen=True)
class ToolContext:
    """What a tool may use: the caller's tenant session and server-side identity. Never model-supplied."""

    session: Session
    organization_id: int
    account_id: int
    role: UserRole
    mission_id: int
    now: datetime
    student_code: Optional[str] = None  # the caller's own profile (from the membership), never from the model
    faculty_profile_id: Optional[int] = None


class ToolResult(BaseModel):
    """A tool's safe, structured result (redacted again before it is persisted)."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: Dict[str, Any] = Field(default_factory=dict)
    error_code: Optional[str] = Field(default=None, max_length=64)


class ToolExecutionError(RuntimeError):
    """An expected tool failure. ``code`` is persisted; the message is not."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AgentTool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[Type[BaseModel]]
    required_roles: ClassVar[FrozenSet[UserRole]]
    side_effecting: ClassVar[bool] = False
    # Internal request tools only: the models this tool may insert/update. Empty = strictly read-only.
    request_models: ClassVar[FrozenSet[type]] = frozenset()

    @abstractmethod
    def execute(self, context: ToolContext, args: BaseModel) -> ToolResult:
        """Run with already-validated ``args`` (an ``input_model`` instance)."""


@dataclass
class AgentRegistry:
    _agents: Dict[str, AgentSpec] = field(default_factory=dict)

    def register(self, spec: AgentSpec) -> None:
        if not re.fullmatch(IDENTIFIER, spec.key):
            raise RegistryError(f"invalid agent key {spec.key!r}")
        if spec.key in self._agents:
            raise RegistryError(f"agent {spec.key!r} is already registered")
        if not spec.supported_roles:
            raise RegistryError(f"agent {spec.key!r} must declare supported roles")
        self._agents[spec.key] = spec

    def get(self, key: str) -> Optional[AgentSpec]:
        return self._agents.get(key)

    def keys(self) -> List[str]:
        return sorted(self._agents)


@dataclass
class ToolRegistry:
    _tools: Dict[str, AgentTool] = field(default_factory=dict)

    def register(self, tool: AgentTool) -> None:
        if not re.fullmatch(TOOL_NAME, getattr(tool, "name", "") or ""):
            raise RegistryError(f"invalid tool name {getattr(tool, 'name', None)!r}")
        if tool.name in self._tools:
            raise RegistryError(f"tool {tool.name!r} is already registered")
        if tool.side_effecting:
            raise RegistryError(f"tool {tool.name!r} has side effects; those run only through the Action Agent and Approval Gate")
        if not (isinstance(tool.input_model, type) and issubclass(tool.input_model, BaseModel)):
            raise RegistryError(f"tool {tool.name!r} needs a Pydantic input model")
        if tool.input_model.model_config.get("extra") != "forbid":
            raise RegistryError(f"tool {tool.name!r} input model must forbid extra fields")
        if any(not (isinstance(m, type) and issubclass(m, Base)) for m in tool.request_models):
            raise RegistryError(f"tool {tool.name!r} request_models must be mapped models")
        if not tool.required_roles:
            raise RegistryError(f"tool {tool.name!r} must declare the roles allowed to use it")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[AgentTool]:
        return self._tools.get(name)

    def describe(self, names: FrozenSet[str], role: UserRole) -> List[ToolDescriptor]:
        return [ToolDescriptor(name=t.name, description=t.description, input_schema=t.input_model.model_json_schema())
                for name in sorted(names) if (t := self._tools.get(name)) is not None and role in t.required_roles]
