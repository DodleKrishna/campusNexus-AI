"""AI usage telemetry, organization AI budgets and the AI Operations / Control Tower figures.

Hackathon fast-finish: lightweight, not billing. Costs are estimates from
``app.llm.router.PRICES_PER_MILLION``; tokens and cost stay NULL (shown as
"unavailable") when the provider did not report token usage.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.ai_usage import AIUsageEvent
from app.db.models.mission import ApprovalRecord, Mission
from app.db.models.organization import Organization
from app.db.tenant_session import TenantSessionFactory, session_organization
from app.llm.router import AIBudgetExceededError, AIContext, no_ai
from app.schemas.enums import IntelligenceLevel

logger = logging.getLogger(__name__)


def _month_start(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_spend(session: Session) -> float:
    """Estimated AI spend of the session's organization this calendar month (tenant-scoped)."""
    total = session.execute(
        select(func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0.0)).where(AIUsageEvent.created_at >= _month_start())
    ).scalar_one()
    return float(total or 0.0)


class AIUsageRecorder:
    """Persists buffered usage per organization and enforces the organization's monthly budget."""

    def __init__(self, factory: TenantSessionFactory) -> None:
        self._factory = factory

    def check_budget(self, organization_id: int) -> None:
        with self._factory.open_tenant_session(organization_id) as session:
            organization = session.get(Organization, organization_id)
            budget = organization.monthly_ai_budget_usd if organization is not None else None
            if budget is None:
                return
            spent = month_spend(session)
        if spent >= budget:
            raise AIBudgetExceededError(organization_id, budget, spent)

    def persist(self, context: AIContext) -> None:
        if context.organization_id is None:
            return  # system mode (scripts/tests): nothing to attribute
        try:
            with self._factory.open_tenant_session(context.organization_id) as session:
                session.add_all([AIUsageEvent(
                    operation=r.operation, level=r.level, model=r.model, provider=r.provider, mission_id=r.mission_id,
                    agent_key=r.agent_key, run_id=r.run_id,
                    input_tokens=r.input_tokens, output_tokens=r.output_tokens, latency_ms=r.latency_ms, success=r.success,
                    error_kind=r.error_kind, estimated_cost_usd=r.estimated_cost_usd, created_at=r.created_at,
                ) for r in context.records])
                session.commit()
        except Exception:  # noqa: BLE001 -- telemetry never breaks the user's request
            logger.exception("could not persist %d AI usage record(s)", len(context.records))


def record_no_ai(session: Session, operation: str, reason: str, *, mission_id: Optional[str] = None) -> None:
    """A request resolved by deterministic logic alone (committed with the caller's transaction)."""
    decision = no_ai(operation, reason)
    session.add(AIUsageEvent(operation=decision.operation, level=IntelligenceLevel.NO_AI, model=None, provider=None,
                             mission_id=mission_id, success=True))


def usage_summary(session: Session) -> Dict[str, Any]:
    rows = session.execute(select(AIUsageEvent)).scalars().all()
    ai = [r for r in rows if r.level != IntelligenceLevel.NO_AI]
    latencies = [r.latency_ms for r in ai if r.latency_ms is not None]
    costs = [r.estimated_cost_usd for r in ai if r.estimated_cost_usd is not None]
    organization_id = session_organization(session)
    organization = session.get(Organization, organization_id) if organization_id is not None else None
    return {
        "total_requests": len(rows),
        "no_ai_count": len(rows) - len(ai),
        "no_ai_percent": round(100 * (len(rows) - len(ai)) / len(rows), 1) if rows else None,
        "light_calls": sum(1 for r in ai if r.level == IntelligenceLevel.LIGHT),
        "advanced_calls": sum(1 for r in ai if r.level == IntelligenceLevel.ADVANCED),
        "estimated_cost_usd": round(sum(costs), 6) if costs else None,
        "cost_available_for": len(costs),
        "average_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
        "success_rate_percent": round(100 * sum(1 for r in ai if r.success) / len(ai), 1) if ai else None,
        "month_spend_usd": round(month_spend(session), 6),
        "monthly_budget_usd": organization.monthly_ai_budget_usd if organization is not None else None,
    }


def control_tower(session: Session, limit: int = 12) -> List[Dict[str, Any]]:
    """Recent missions with the AI they used (from telemetry) and whether an approval was needed."""
    missions = session.execute(select(Mission).order_by(Mission.created_at.desc()).limit(limit)).scalars().all()
    ids = [m.mission_id for m in missions]
    events: Dict[str, List[AIUsageEvent]] = {}
    for event in session.execute(select(AIUsageEvent).where(AIUsageEvent.mission_id.in_(ids))).scalars() if ids else []:
        events.setdefault(event.mission_id, []).append(event)
    approvals = dict(session.execute(
        select(ApprovalRecord.mission_id, func.count()).where(ApprovalRecord.mission_id.in_(ids)).group_by(ApprovalRecord.mission_id)
    ).all()) if ids else {}
    organization_names = {o.id: o.name for o in session.execute(select(Organization)).scalars()}
    out = []
    for mission in missions:
        used = events.get(mission.mission_id, [])
        ai = [e for e in used if e.level != IntelligenceLevel.NO_AI]
        models = sorted({e.model for e in ai if e.model})
        costs = [e.estimated_cost_usd for e in ai if e.estimated_cost_usd is not None]
        out.append({
            "mission_id": mission.mission_id, "status": mission.status.value, "role": mission.user_role.value,
            "organization": organization_names.get(mission.organization_id), "goal": mission.original_goal,
            "created_at": mission.created_at,
            "intelligence": ("advanced" if any(e.level == IntelligenceLevel.ADVANCED for e in ai)
                             else "light" if ai else "no_ai"),
            "models": models or ["NO-AI"], "ai_calls": len(ai),
            "estimated_cost_usd": round(sum(costs), 6) if costs else None,
            "latency_ms": sum(e.latency_ms or 0 for e in ai) if ai else None,
            "approvals_required": int(approvals.get(mission.mission_id, 0)),
        })
    return out
