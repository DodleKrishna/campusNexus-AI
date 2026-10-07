"""AgentOS V2 Phase 1: durable autonomous missions, their steps, and domain events.

Separate from the Phase 4+ ``missions``/``mission_steps`` tables (the LangGraph
Mission Orchestrator): an ``AgentMission`` is advanced one bounded transition at a
time by ``app.agentos.runtime.AgentRuntime``. All three tables are tenant-owned.

No column ever holds hidden reasoning or chain-of-thought: steps store only
structured, redacted summaries of the observable decision, action and result.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, UTCDateTime, portable_enum, utc_now


class AgentMissionStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_EVENT = "waiting_event"
    WAITING_HUMAN = "waiting_human"
    WAITING_CONNECTIVITY = "waiting_connectivity"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_MISSION_STATUSES = frozenset({AgentMissionStatus.COMPLETED, AgentMissionStatus.FAILED, AgentMissionStatus.CANCELLED})
WAITING_MISSION_STATUSES = frozenset({
    AgentMissionStatus.WAITING_EVENT, AgentMissionStatus.WAITING_HUMAN, AgentMissionStatus.WAITING_CONNECTIVITY,
})


class AgentStepStatus(str, enum.Enum):
    EXECUTED = "executed"
    REJECTED = "rejected"  # the decision failed validation/allowlists; nothing was executed
    FAILED = "failed"  # the action was attempted and failed


class AgentMission(TenantMixin, Base):
    __tablename__ = "agent_missions"
    __table_args__ = (
        # An owner's missions of one agent, newest first (Nexus list_missions / list_active_missions).
        Index("ix_agent_missions_org_owner_agent", "organization_id", "owner_account_id", "agent_key"),
        # Waiting missions whose wake timer is due (status IN waiting AND next_wake_at <= now): the scan a
        # background waker runs; also serves per-status listings.
        Index("ix_agent_missions_org_status_wake", "organization_id", "status", "next_wake_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_account_id: Mapped[int] = mapped_column(ForeignKey("auth_accounts.id"), index=True)
    created_by_account_id: Mapped[int] = mapped_column(ForeignKey("auth_accounts.id"))
    agent_key: Mapped[str] = mapped_column(String(40))
    goal: Mapped[str] = mapped_column(Text)
    success_criteria: Mapped[List[str]] = mapped_column(JSON, default=list)
    status: Mapped[AgentMissionStatus] = mapped_column(portable_enum(AgentMissionStatus), default=AgentMissionStatus.PENDING,
                                                      index=True)
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    # Redacted, structured working state: inputs, recent observations, outcome. Never reasoning.
    context: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    current_plan: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, default=None)
    step_count: Mapped[int] = mapped_column(Integer, default=0)
    max_steps: Mapped[int] = mapped_column(Integer, default=20)
    next_wake_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    waiting_for: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)


class AgentStep(TenantMixin, Base):
    __tablename__ = "agent_steps"
    # One row per transition: a concurrent duplicate run of the same step fails on insert.
    __table_args__ = (Index("ux_agent_steps_mission_step", "mission_id", "step_number", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mission_id: Mapped[int] = mapped_column(ForeignKey("agent_missions.id"), index=True)
    step_number: Mapped[int] = mapped_column(Integer)
    agent_key: Mapped[str] = mapped_column(String(40))
    action_type: Mapped[str] = mapped_column(String(20))  # the decision kind
    tool_name: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    delegated_agent: Mapped[Optional[str]] = mapped_column(String(40), default=None)
    input_summary: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    output_summary: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[AgentStepStatus] = mapped_column(portable_enum(AgentStepStatus))
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class DomainEvent(TenantMixin, Base):
    """A durable fact that happened (published by deterministic code), consumed at most once."""

    __tablename__ = "domain_events"
    # Unconsumed events of one type (EventBus.pending / next_for_mission: consumed_at IS NULL AND event_type = ...).
    __table_args__ = (Index("ix_domain_events_org_type_consumed", "organization_id", "event_type", "consumed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    actor_account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("auth_accounts.id"), default=None)
    subject_type: Mapped[Optional[str]] = mapped_column(String(40), default=None)
    subject_id: Mapped[Optional[str]] = mapped_column(String(40), default=None, index=True)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
