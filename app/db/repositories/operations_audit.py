"""Append-only audit trail for Phase 16 campus operations (attendance, workflow requests).

Part of the Context Service's audit store: mission events stay in
``audit_logs``; operations that have no mission are recorded here. Rows are
only ever added, never updated or deleted.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.workflow import OperationAuditEvent


def record(
    session: Session, *, event_type: str, actor_account_id: Optional[int], actor_role: str, subject_type: str,
    subject_id: str, message: str, metadata: Optional[Dict[str, Any]] = None, at: Optional[datetime] = None,
) -> OperationAuditEvent:
    """Stage an audit row in the caller's transaction (committed with the change it records)."""
    event = OperationAuditEvent(
        event_type=event_type, actor_account_id=actor_account_id, actor_role=actor_role, subject_type=subject_type,
        subject_id=subject_id, message=message, event_metadata=metadata or {},
    )
    if at is not None:
        event.timestamp = at
    session.add(event)
    return event


def events_for(session: Session, subject_type: str, subject_id: str) -> List[OperationAuditEvent]:
    return list(
        session.execute(
            select(OperationAuditEvent)
            .where(OperationAuditEvent.subject_type == subject_type, OperationAuditEvent.subject_id == subject_id)
            .order_by(OperationAuditEvent.id)
        ).scalars().all()
    )
