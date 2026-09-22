"""Read-only mission/context repository functions.

Writes to these tables go through app.services.context.ContextService, which
owns the mission/audit lifecycle rules (e.g. append-only audit log, no silent
approval expiry). This module is for plain lookups only.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.mission import AuditLog, Mission, MissionStep


def get_mission(session: Session, mission_id: str) -> Mission | None:
    stmt = select(Mission).where(Mission.mission_id == mission_id)
    return session.execute(stmt).scalar_one_or_none()


def get_mission_steps(session: Session, mission_id: str) -> list[MissionStep]:
    stmt = (
        select(MissionStep)
        .where(MissionStep.mission_id == mission_id)
        .order_by(MissionStep.sequence)
    )
    return list(session.execute(stmt).scalars().all())


def get_audit_trail(session: Session, mission_id: str) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.mission_id == mission_id).order_by(AuditLog.id)
    return list(session.execute(stmt).scalars().all())
