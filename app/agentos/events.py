"""Durable domain events (``domain_events``) in the caller's tenant session.

``publish`` stages a row (committed with the caller's transaction); event types
come from the ``DomainEventType`` allowlist and payloads are redacted. A mission
waiting for an event consumes the oldest unconsumed matching event addressed to
it (subject ``agent_mission``) or to its owner (subject ``account``) -- at most once.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.agentos.safety import bounded
from app.agentos.schemas import DomainEventType
from app.db.models.agent_kernel import TERMINAL_MISSION_STATUSES, AgentMission, DomainEvent

SUBJECT_MISSION, SUBJECT_ACCOUNT = "agent_mission", "account"
_SUBJECT_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class EventService:
    def publish(
        self, session: Session, *, event_type: DomainEventType | str, payload: Optional[Dict[str, Any]] = None,
        actor_account_id: Optional[int] = None, subject_type: Optional[str] = None, subject_id: Optional[str | int] = None,
        now: Optional[datetime] = None,
    ) -> DomainEvent:
        kind = DomainEventType(event_type)  # ValueError for anything outside the allowlist
        if subject_type is not None and not _SUBJECT_TYPE.fullmatch(subject_type):
            raise ValueError("invalid subject_type")
        event = DomainEvent(event_type=kind.value, actor_account_id=actor_account_id, subject_type=subject_type,
                            subject_id=None if subject_id is None else str(subject_id)[:40], payload=bounded(payload or {}))
        if now is not None:
            event.created_at = now
        session.add(event)
        session.flush()
        return event

    def pending(self, session: Session, *, event_type: Optional[str] = None, subject_type: Optional[str] = None,
                subject_id: Optional[str | int] = None, limit: int = 50) -> List[DomainEvent]:
        query = select(DomainEvent).where(DomainEvent.consumed_at.is_(None))
        if event_type is not None:
            query = query.where(DomainEvent.event_type == event_type)
        if subject_type is not None:
            query = query.where(DomainEvent.subject_type == subject_type)
        if subject_id is not None:
            query = query.where(DomainEvent.subject_id == str(subject_id))
        return list(session.execute(query.order_by(DomainEvent.id).limit(limit)).scalars())

    def next_for_mission(self, session: Session, mission: AgentMission) -> Optional[DomainEvent]:
        if not mission.waiting_for:
            return None
        return session.execute(
            select(DomainEvent).where(
                DomainEvent.consumed_at.is_(None), DomainEvent.event_type == mission.waiting_for,
                or_(and_(DomainEvent.subject_type == SUBJECT_MISSION, DomainEvent.subject_id == str(mission.id)),
                    and_(DomainEvent.subject_type == SUBJECT_ACCOUNT, DomainEvent.subject_id == str(mission.owner_account_id))),
            ).order_by(DomainEvent.id).limit(1)
        ).scalars().first()

    def consume(self, event: DomainEvent, now: datetime) -> None:
        if event.consumed_at is not None:
            raise ValueError("event already consumed")
        event.consumed_at = now


def wake_mission(session: Session, mission_id: Optional[int], now: datetime) -> None:
    """Bring a non-terminal mission's wake time forward to ``now`` (the due-mission worker picks it up).

    This is how a domain event wakes a blocked mission earlier than its checkpoint: the event is addressed to
    the mission, its wake time moves to now, and the agent's supervisor decides on the next worker run whether
    the change needs an AI decision at all."""
    mission = session.get(AgentMission, mission_id) if mission_id is not None else None
    if mission is not None and mission.status not in TERMINAL_MISSION_STATUSES and (
            mission.next_wake_at is None or mission.next_wake_at > now):
        mission.next_wake_at = now
