"""AgentRegistry and ToolRegistry: the only things a decision can name.

Agents and tools are registered by code at startup, never by a model. A decision
names a tool or agent by identifier; the runtime looks it up here and in the
calling agent's allowlist, so a model can never reach an import path, URL,
shell command, SQL statement or unregistered tool.

Phase 1 registers read-only tools only: a side-effecting tool is refused, so
writes keep going through the Verifier -> Approval Gate -> Action Agent path
until a later phase wires that path into the kernel.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional, Type

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.agentos.schemas import IDENTIFIER, TOOL_NAME, ToolDescriptor
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
        if not tool.required_roles:
            raise RegistryError(f"tool {tool.name!r} must declare the roles allowed to use it")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[AgentTool]:
        return self._tools.get(name)

    def describe(self, names: FrozenSet[str], role: UserRole) -> List[ToolDescriptor]:
        return [ToolDescriptor(name=t.name, description=t.description, input_schema=t.input_model.model_json_schema())
                for name in sorted(names) if (t := self._tools.get(name)) is not None and role in t.required_roles]
