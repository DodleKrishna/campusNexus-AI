"""Mission / Context Service persistence models.

These are the durable, relational shadow of what the Context Service tracks:
mission + step lifecycle, per-agent run history, tool call records, approval
records, the audit trail, and freeform memory. They deliberately do not
duplicate the Phase 1 Pydantic contracts in app/schemas -- those are the
in-flight structured-output contracts; these are what gets persisted.

Mission/MissionStep/ApprovalRecord reuse the same string ids
(mission_id/step_id/approval_id) that the Pydantic schemas define, so a
Context Service call site never has to translate between a DB surrogate key
and the id used across component boundaries.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import Enum as SAEnum, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UTCDateTime, utc_now
from app.schemas.enums import (
    AgentName,
    AgentResultStatus,
    ApprovalStatus,
    MissionStatus,
    TaskStatus,
    ToolAccessType,
    ToolExecutionStatus,
    UserRole,
)


def _enum_column(enum_cls: type) -> SAEnum:
    return SAEnum(enum_cls, values_callable=lambda e: [m.value for m in e])


class Mission(Base):
    __tablename__ = "missions"

    mission_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    user_role: Mapped[UserRole] = mapped_column(_enum_column(UserRole))
    original_goal: Mapped[str] = mapped_column(Text)
    normalized_goal: Mapped[Optional[str]] = mapped_column(Text, default=None)
    status: Mapped[MissionStatus] = mapped_column(_enum_column(MissionStatus), default=MissionStatus.PENDING)
    final_result: Mapped[Optional[str]] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    steps: Mapped[List["MissionStep"]] = relationship(back_populates="mission", cascade="all, delete-orphan")


class MissionStep(Base):
    __tablename__ = "mission_steps"

    step_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.mission_id"), index=True)
    agent: Mapped[AgentName] = mapped_column(_enum_column(AgentName))
    objective: Mapped[str] = mapped_column(Text)
    sequence: Mapped[int] = mapped_column(default=0)
    status: Mapped[TaskStatus] = mapped_column(_enum_column(TaskStatus), default=TaskStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    started_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    completed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)

    mission: Mapped["Mission"] = relationship(back_populates="steps")


class AgentRun(Base):
    """One recorded execution of an agent against a mission step.

    ``facts``/``errors`` mirror the free-form ``Dict[str, JsonValue]``/``List[str]``
    shape of the Phase 1 ``AgentResult`` schema; unlike the domain tables above,
    this data has no fixed relational shape to normalize into, so JSON storage
    is the appropriate (not merely convenient) representation here.
    """

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.mission_id"), index=True)
    step_id: Mapped[str] = mapped_column(ForeignKey("mission_steps.step_id"), index=True)
    agent: Mapped[AgentName] = mapped_column(_enum_column(AgentName))
    status: Mapped[AgentResultStatus] = mapped_column(_enum_column(AgentResultStatus))
    facts: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    errors: Mapped[List[str]] = mapped_column(JSON, default=list)
    started_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    completed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    mission = relationship("Mission")
    step = relationship("MissionStep")


class ToolCallRecord(Base):
    __tablename__ = "tool_call_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    tool_call_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.mission_id"), index=True)
    step_id: Mapped[str] = mapped_column(ForeignKey("mission_steps.step_id"), index=True)
    tool_name: Mapped[str] = mapped_column(String(100))
    read_or_write: Mapped[ToolAccessType] = mapped_column(_enum_column(ToolAccessType))
    arguments: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(150), default=None, index=True)
    status: Mapped[ToolExecutionStatus] = mapped_column(
        _enum_column(ToolExecutionStatus), default=ToolExecutionStatus.PENDING
    )
    result_data: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=None)
    error: Mapped[Optional[str]] = mapped_column(Text, default=None)
    postcondition_verified: Mapped[Optional[bool]] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    executed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)

    mission = relationship("Mission")
    step = relationship("MissionStep")


class ApprovalRecord(Base):
    __tablename__ = "approval_records"

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.mission_id"), index=True)
    step_id: Mapped[str] = mapped_column(ForeignKey("mission_steps.step_id"), index=True)
    tool_call_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("tool_call_records.tool_call_id"), default=None
    )
    action_summary: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(String(120))
    status: Mapped[ApprovalStatus] = mapped_column(_enum_column(ApprovalStatus), default=ApprovalStatus.PENDING)
    decision_by: Mapped[Optional[str]] = mapped_column(String(120), default=None)
    decision_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    decision_reason: Mapped[Optional[str]] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    mission = relationship("Mission")
    step = relationship("MissionStep")


class AuditLog(Base):
    """Append-only audit trail. Ordering by ``id`` (insertion order) is authoritative."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.mission_id"), index=True)
    step_id: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    event_type: Mapped[str] = mapped_column(String(80))
    actor: Mapped[str] = mapped_column(String(120))
    message: Mapped[str] = mapped_column(Text)
    event_metadata: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    mission = relationship("Mission")


class MemoryRecord(Base):
    """Freeform memory scoped to a student and/or mission, for future recall."""

    __tablename__ = "memory_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    student_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("students.student_code"), index=True, default=None
    )
    mission_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("missions.mission_id"), default=None, index=True
    )
    category: Mapped[str] = mapped_column(String(60))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
