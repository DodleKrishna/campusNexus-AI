"""Phase 18: in-app notifications for accounts with no student/faculty profile (administrators).

Staged in the caller's transaction. ``ref_key`` makes a notification idempotent
per recipient.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.auth import AuthAccount
from app.db.models.communication import AccountNotification, NotificationStatus
from app.schemas.department import StaffNotificationItem
from app.schemas.enums import UserRole


def notify(
    session: Session, account_id: int, title: str, body: str, category: str, *, now: datetime, ref_key: Optional[str] = None,
) -> Optional[AccountNotification]:
    if ref_key is not None and session.execute(select(AccountNotification.id).where(
        AccountNotification.account_id == account_id, AccountNotification.ref_key == ref_key)).first() is not None:
        return None
    note = AccountNotification(account_id=account_id, title=title, body=body, category=category,
                               status=NotificationStatus.SENT, ref_key=ref_key, created_at=now)
    session.add(note)
    return note


def notify_admins(session: Session, title: str, body: str, category: str, *, now: datetime, ref_key: Optional[str] = None) -> int:
    admins = session.execute(
        select(AuthAccount.id).where(AuthAccount.role == UserRole.ADMIN, AuthAccount.is_active.is_(True))
    ).scalars().all()
    return sum(1 for account_id in admins if notify(session, account_id, title, body, category, now=now, ref_key=ref_key))


def list_for(session: Session, account_id: int, limit: int = 30) -> List[StaffNotificationItem]:
    rows = session.execute(
        select(AccountNotification).where(AccountNotification.account_id == account_id)
        .order_by(AccountNotification.created_at.desc(), AccountNotification.id.desc()).limit(limit)
    ).scalars().all()
    return [StaffNotificationItem(id=n.id, title=n.title, body=n.body, category=n.category, status=n.status.value, created_at=n.created_at) for n in rows]
