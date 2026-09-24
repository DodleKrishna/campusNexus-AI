"""Phase 17: in-app notifications for faculty and HODs (``staff_notifications``).

Staged in the caller's transaction. A ``ref_key`` makes a generated warning
idempotent: the same key for the same recipient is recorded once.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.communication import NotificationStatus, StaffNotification
from app.schemas.department import StaffNotificationItem


def notify(
    session: Session, faculty_id: int, title: str, body: str, category: str, *, now: datetime, ref_key: Optional[str] = None,
) -> Optional[StaffNotification]:
    if ref_key is not None:
        exists = session.execute(
            select(StaffNotification.id).where(StaffNotification.faculty_id == faculty_id, StaffNotification.ref_key == ref_key)
        ).first()
        if exists is not None:
            return None
    note = StaffNotification(
        faculty_id=faculty_id, title=title, body=body, category=category, status=NotificationStatus.SENT,
        ref_key=ref_key, created_at=now,
    )
    session.add(note)
    return note


def list_for(session: Session, faculty_id: int, limit: int = 30) -> List[StaffNotificationItem]:
    rows = session.execute(
        select(StaffNotification).where(StaffNotification.faculty_id == faculty_id)
        .order_by(StaffNotification.created_at.desc(), StaffNotification.id.desc()).limit(limit)
    ).scalars().all()
    return [
        StaffNotificationItem(id=n.id, title=n.title, body=n.body, category=n.category, status=n.status.value, created_at=n.created_at)
        for n in rows
    ]
