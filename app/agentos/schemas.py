"""Typed AgentOS primitives: what an AgentBrain sees (context/observations) and may return (a decision).

``AgentDecision`` is the only shape a brain can answer with. It forbids extra
fields (so no ``reasoning``/``thought`` can ride along) and each kind allows only
its own fields. Names are identifiers, never import paths, URLs, SQL or code.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.models.agent_kernel import AgentMissionStatus, AgentStepStatus

IDENTIFIER = r"^[a-z][a-z0-9_]{0,39}$"
TOOL_NAME = r"^[a-z][a-z0-9_]{0,63}$"
USER_MESSAGE_MAX = 800


class DecisionKind(str, enum.Enum):
    TOOL = "tool"
    DELEGATE = "delegate"
    WAIT = "wait"
    ASK_HUMAN = "ask_human"
    REPLAN = "replan"
    COMPLETE = "complete"
    FAIL = "fail"


class DomainEventType(str, enum.Enum):
    """Allowlist of durable event types. Domain workflows that publish most of these come in later phases."""

    ASSIGNMENT_SUBMITTED = "ASSIGNMENT_SUBMITTED"
    EXAM_STARTED = "EXAM_STARTED"
    ATTENDANCE_MARKED = "ATTENDANCE_MARKED"
    ABSENCE_DETECTED = "ABSENCE_DETECTED"
    CALL_ANSWERED = "CALL_ANSWERED"
    NETWORK_RESTORED = "NETWORK_RESTORED"
    HUMAN_RESPONDED = "HUMAN_RESPONDED"
    DELEGATION_FINISHED = "DELEGATION_FINISHED"
    # Phase 3: the assignment domain.
    ASSIGNMENT_PUBLISHED = "ASSIGNMENT_PUBLISHED"
    ASSIGNMENT_CANCELLED = "ASSIGNMENT_CANCELLED"
    COMMUNICATION_REQUESTED = "COMMUNICATION_REQUESTED"
    # Phase 4: the exam domain (EXAM_STARTED above).
    EXAM_SCHEDULED = "EXAM_SCHEDULED"
    EXAM_ATTENDANCE_MARKED = "EXAM_ATTENDANCE_MARKED"
    MAKEUP_EXAM_COMPLETED = "MAKEUP_EXAM_COMPLETED"
    EXAM_ENDED = "EXAM_ENDED"
    EXAM_CANCELLED = "EXAM_CANCELLED"
    # Phase 4: class attendance (ATTENDANCE_MARKED / ABSENCE_DETECTED above). A future biometric/ERP connector
    # publishes these same types.
    CLASS_STARTED = "CLASS_STARTED"
    ATTENDANCE_UPDATED = "ATTENDANCE_UPDATED"
    ATTENDANCE_JUSTIFIED = "ATTENDANCE_JUSTIFIED"
    ATTENDANCE_CORRECTED = "ATTENDANCE_CORRECTED"
    LEAVE_APPROVED = "LEAVE_APPROVED"


# Which fields each decision kind may (and must) carry.
_KIND_FIELDS: Dict[DecisionKind, tuple[set[str], set[str]]] = {
    DecisionKind.TOOL: ({"tool_name"}, {"tool_name", "tool_input"}),
    DecisionKind.DELEGATE: ({"delegate_agent", "delegate_goal"}, {"delegate_agent", "delegate_goal"}),
    DecisionKind.WAIT: ({"wait_for"}, {"wait_for", "wake_after_seconds"}),
    DecisionKind.ASK_HUMAN: ({"question"}, {"question", "user_message"}),
    DecisionKind.REPLAN: ({"plan"}, {"plan"}),
    DecisionKind.COMPLETE: ({"outcome"}, {"outcome", "user_message"}),
    DecisionKind.FAIL: ({"reason"}, {"reason"}),
}
_PAYLOAD_FIELDS = {f for _, allowed in _KIND_FIELDS.values() for f in allowed}


class AgentDecision(BaseModel):
    """One structured, observable decision. Validated before anything is executed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: DecisionKind
    tool_name: Optional[str] = Field(default=None, pattern=TOOL_NAME)
    tool_input: Dict[str, Any] = Field(default_factory=dict)
    delegate_agent: Optional[str] = Field(default=None, pattern=IDENTIFIER)
    delegate_goal: Optional[str] = Field(default=None, min_length=1, max_length=500)
    wait_for: Optional[DomainEventType] = None
    wake_after_seconds: Optional[int] = Field(default=None, ge=1, le=7 * 24 * 3600)
    question: Optional[str] = Field(default=None, min_length=1, max_length=500)
    plan: Optional[List[str]] = Field(default=None, min_length=1, max_length=20)
    outcome: Optional[str] = Field(default=None, min_length=1, max_length=1000)
    reason: Optional[str] = Field(default=None, min_length=1, max_length=300)
    # Phase 2: the short, user-visible reply of a COMPLETE / ASK_HUMAN decision. Not reasoning.
    user_message: Optional[str] = Field(default=None, min_length=1, max_length=USER_MESSAGE_MAX)

    @model_validator(mode="after")
    def _fields_match_kind(self) -> "AgentDecision":
        required, allowed = _KIND_FIELDS[self.kind]
        present = {f for f in _PAYLOAD_FIELDS if getattr(self, f) not in (None, {}, [])}
        if missing := required - present:
            raise ValueError(f"{self.kind.value} decision requires {sorted(missing)}")
        if extra := present - allowed:
            raise ValueError(f"{self.kind.value} decision may not set {sorted(extra)}")
        if self.plan and any(not step or len(step) > 200 for step in self.plan):
            raise ValueError("plan steps must be 1-200 characters")
        return self


class AgentObservation(BaseModel):
    """Something the agent can see: a previous result, a consumed event, a wake-up. Redacted data only."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["mission_started", "tool_result", "tool_error", "delegated", "event", "wake", "plan_updated"]
    source: str = Field(max_length=64)
    data: Dict[str, Any] = Field(default_factory=dict)
    at: datetime


class ToolDescriptor(BaseModel):
    name: str
    description: str
    input_schema: Dict[str, Any]


class AgentContext(BaseModel):
    """Everything an AgentBrain is given. Built by the runtime from the mission; no DB handle, no secrets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mission_id: int
    agent_key: str
    goal: str
    success_criteria: List[str]
    status: AgentMissionStatus
    step_count: int
    max_steps: int
    plan: Optional[Dict[str, Any]] = None
    state: Dict[str, Any] = Field(default_factory=dict)
    observations: List[AgentObservation] = Field(default_factory=list)
    allowed_tools: List[ToolDescriptor] = Field(default_factory=list)
    allowed_delegate_agents: List[str] = Field(default_factory=list)
    caller_role: Optional[str] = None  # the server-resolved role of the human the mission acts for
    # Phase 3: the decision kinds / wait events this agent may use (empty: unrestricted, the Phase 1-2 default).
    allowed_decisions: List[str] = Field(default_factory=list)
    wait_events: List[str] = Field(default_factory=list)


class AgentResult(BaseModel):
    """The outcome of one ``run_step`` call."""

    mission_id: int
    transitioned: bool  # False: nothing ran (still waiting / brain unavailable)
    status: AgentMissionStatus
    step_number: Optional[int] = None
    decision_kind: Optional[DecisionKind] = None
    step_status: Optional[AgentStepStatus] = None
    error_code: Optional[str] = None
    detail: Optional[str] = None


# --- API bodies / views --------------------------------------------------------------------------------------------


class CreateAgentMission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_key: str = Field(pattern=IDENTIFIER)
    goal: str = Field(min_length=1, max_length=1000)
    success_criteria: List[str] = Field(default_factory=list, max_length=10)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    max_steps: int = Field(default=20, ge=1, le=100)
    context: Dict[str, Any] = Field(default_factory=dict)


class AgentMissionView(BaseModel):
    id: int
    agent_key: str
    goal: str
    success_criteria: List[str]
    status: AgentMissionStatus
    priority: str
    context: Dict[str, Any]
    current_plan: Optional[Dict[str, Any]]
    step_count: int
    max_steps: int
    next_wake_at: Optional[datetime]
    waiting_for: Optional[str]
    owner_account_id: int
    created_by_account_id: int
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime]


class AgentStepView(BaseModel):
    id: int
    step_number: int
    agent_key: str
    action_type: str
    tool_name: Optional[str]
    delegated_agent: Optional[str]
    input_summary: Dict[str, Any]
    output_summary: Dict[str, Any]
    status: AgentStepStatus
    latency_ms: Optional[int]
    created_at: datetime
