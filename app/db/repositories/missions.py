"""Read-only mission/context repository functions.

Writes to these tables go through app.services.context.ContextService, which
owns the mission/audit lifecycle rules (e.g. append-only audit log, no silent
approval expiry). This module is for plain lookups only.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.mission import ApprovalRecord, AuditLog, Mission, MissionStep, ToolCallRecord


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


def get_latest_approval_for_step(session: Session, step_id: str) -> ApprovalRecord | None:
    """The most recently created approval for a mission step, or None.

    "Most recent" is what lets ``ApprovalGate.propose_edit`` supersede an
    earlier approval with a fresh one for the same step -- the Action Agent
    always re-dispatch-checks against this one, never an older row.
    """
    stmt = (
        select(ApprovalRecord)
        .where(ApprovalRecord.step_id == step_id)
        .order_by(ApprovalRecord.created_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def get_tool_call_by_id(session: Session, tool_call_id: str) -> ToolCallRecord | None:
    stmt = select(ToolCallRecord).where(ToolCallRecord.tool_call_id == tool_call_id)
    return session.execute(stmt).scalar_one_or_none()


def get_tool_call_by_idempotency_key(session: Session, idempotency_key: str) -> ToolCallRecord | None:
    stmt = select(ToolCallRecord).where(ToolCallRecord.idempotency_key == idempotency_key)
    return session.execute(stmt).scalar_one_or_none()


def count_approvals_for_step(session: Session, step_id: str) -> int:
    """How many approval requests a step has had -- the versioned-idempotency-key
    input for ApprovalGate/ActionAgent's edit-supersedes-original flow."""
    from sqlalchemy import func

    stmt = select(func.count()).select_from(ApprovalRecord).where(ApprovalRecord.step_id == step_id)
    return session.execute(stmt).scalar_one()
