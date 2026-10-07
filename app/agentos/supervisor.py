"""MissionSupervisor: deterministic code an agent can attach to the kernel (AgentOS V2 Phase 3).

The runtime asks the supervisor, before every brain call, to ``review`` the mission
against facts SQL can determine (success, cancellation, deadline). A terminal verdict
ends the mission without any AI call; a ``wait`` verdict re-arms the timer without one
(e.g. an early wake by an event that does not change what the brain should decide). Only
``continue`` reaches the brain, together with the supervisor's compact ``state``.

The supervisor also gates the brain's own decisions: a COMPLETE is executed only when
``verify_complete`` confirms it, and every WAIT is scheduled by ``plan_wait`` (deterministic
checkpoints), so a model can never declare success early or sleep past a deadline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Literal, Optional, Protocol, Tuple

from sqlalchemy.orm import Session

from app.agentos.schemas import DomainEventType
from app.db.models.agent_kernel import AgentMission

Trigger = Literal["started", "event", "wake", "running"]


@dataclass(frozen=True)
class SupervisorVerdict:
    action: Literal["continue", "wait", "complete", "cancel", "fail"]
    state: Dict[str, Any] = field(default_factory=dict)  # compact facts shown to the brain (continue)
    outcome: Dict[str, Any] = field(default_factory=dict)  # structured result (terminal verdicts)
    waiting_for: Optional[DomainEventType] = None  # wait
    wake_at: Optional[datetime] = None  # wait
    code: Optional[str] = None  # terminal verdicts: a short machine code


class MissionSupervisor(Protocol):
    def review(self, session: Session, mission: AgentMission, now: datetime, trigger: Trigger) -> SupervisorVerdict:
        """Deterministic checks before the brain is asked. May stage writes in the caller's transaction."""

    def verify_complete(self, session: Session, mission: AgentMission, now: datetime) -> bool:
        """True only when the mission's success condition holds right now."""

    def plan_wait(self, session: Session, mission: AgentMission, requested_wake: Optional[datetime],
                  now: datetime) -> Tuple[DomainEventType, Optional[datetime]]:
        """The event to wait for and the next wake time (the brain's request is advisory)."""
