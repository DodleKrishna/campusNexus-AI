"""Agent-as-a-Product MVP: deploy, configure and gate the catalog's product agents per organization.

Every query runs in the caller's tenant session, so an organization only ever
sees and changes its own deployments (another organization's id is "not
found"). Only ``agent_catalog.TEMPLATES`` can be deployed; internal runtime
components never can. The runtime gate (``require_active``) refuses a product
agent that is not deployed, is paused, or does not allow the caller's role --
with an explicit code, never a fabricated answer.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.agent_deployment import DEPLOYMENT_ACTIVE, AgentDeployment
from app.db.models.ai_usage import AIUsageEvent
from app.db.models.auth import AuthAccount
from app.db.models.organization import Organization
from app.db.repositories import operations_audit
from app.schemas.admin_console import AgentRunRow, CatalogEntry, DeploymentConfig, DeploymentView
from app.schemas.enums import IntelligenceLevel
from app.services.agent_catalog import INTERNAL_KEYS, TEMPLATES

AGENT_NOT_DEPLOYED, AGENT_DISABLED, AGENT_ROLE_NOT_ALLOWED = "AGENT_NOT_DEPLOYED", "AGENT_DISABLED", "AGENT_ROLE_NOT_ALLOWED"

# Chat endpoint keys -> catalog agent keys. Keys not listed (e.g. the Permission Agent) are not catalog products.
CHAT_KEY_TO_AGENT: Dict[str, str] = {
    "academic": "academic", "events": "events", "placements": "career", "complaints": "campus_services", "enquiry": "enquiry",
}


class DeploymentError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


# --- Seed ----------------------------------------------------------------------------------------------------


def ensure_default_deployments(session: Session, organization_id: int) -> int:
    """Deploy every catalog agent with its defaults for ``organization_id`` (idempotent; seed / demo reset)."""
    existing = set(session.execute(
        select(AgentDeployment.agent_key).where(AgentDeployment.organization_id == organization_id)).scalars())
    created = 0
    for key, template in TEMPLATES.items():
        if key in existing:
            continue
        session.add(AgentDeployment(
            organization_id=organization_id, agent_key=key, display_name=template.name, status=DEPLOYMENT_ACTIVE,
            intelligence_level=IntelligenceLevel(template.intelligence_level), requires_approval=template.requires_approval,
            allowed_roles=list(template.allowed_roles),
        ))
        created += 1
    session.flush()
    return created


# --- Views ---------------------------------------------------------------------------------------------------


def _stats(session: Session) -> Dict[str, dict]:
    """Runs, success rate and estimated cost per deployed agent, from telemetry (one query)."""
    runs: Dict[str, Dict[str, dict]] = {}
    for event in session.execute(select(AIUsageEvent).where(AIUsageEvent.agent_key.is_not(None))).scalars():
        run = runs.setdefault(event.agent_key, {}).setdefault(event.run_id or f"event-{event.id}", {"ok": True, "cost": None})
        run["ok"] &= bool(event.success)
        if event.estimated_cost_usd is not None:
            run["cost"] = (run["cost"] or 0.0) + event.estimated_cost_usd
    out = {}
    for key, by_run in runs.items():
        costs = [r["cost"] for r in by_run.values() if r["cost"] is not None]
        out[key] = {"runs": len(by_run), "success_rate_percent": round(100 * sum(r["ok"] for r in by_run.values()) / len(by_run), 1),
                    "estimated_cost_usd": round(sum(costs), 6) if costs else None}
    return out


def _view(deployment: AgentDeployment, stats: Dict[str, dict]) -> DeploymentView:
    s = stats.get(deployment.agent_key, {})
    return DeploymentView(
        id=deployment.id, agent_key=deployment.agent_key, display_name=deployment.display_name, status=deployment.status,
        intelligence_level=deployment.intelligence_level.value, monthly_budget_usd=deployment.monthly_budget_usd,
        requires_approval=deployment.requires_approval, allowed_roles=list(deployment.allowed_roles or []),
        runs=s.get("runs", 0), success_rate_percent=s.get("success_rate_percent"), estimated_cost_usd=s.get("estimated_cost_usd"),
        updated_at=deployment.updated_at,
    )


def _deployments(session: Session) -> Dict[str, AgentDeployment]:
    return {d.agent_key: d for d in session.execute(select(AgentDeployment).order_by(AgentDeployment.id)).scalars()}


def catalog(session: Session) -> List[CatalogEntry]:
    deployed, stats = _deployments(session), _stats(session)
    return [CatalogEntry(**template.model_dump(), deployment=_view(deployed[key], stats) if key in deployed else None)
            for key, template in TEMPLATES.items()]


def deployments(session: Session) -> List[DeploymentView]:
    stats = _stats(session)
    return [_view(d, stats) for d in _deployments(session).values()]


def agent_runs(session: Session, limit: int = 12) -> List[AgentRunRow]:
    """Recent runs of deployed agents for the Control Tower (from telemetry)."""
    events = session.execute(
        select(AIUsageEvent).where(AIUsageEvent.run_id.is_not(None)).order_by(AIUsageEvent.created_at.desc()).limit(200)
    ).scalars().all()
    runs: Dict[str, List[AIUsageEvent]] = {}
    for event in events:
        runs.setdefault(event.run_id, []).append(event)
    deployed = _deployments(session)
    organization_names = {o.id: o.name for o in session.execute(select(Organization)).scalars()}
    rows = []
    for run_id, used in list(runs.items())[:limit]:
        first = used[-1]
        deployment = deployed.get(first.agent_key)
        costs = [e.estimated_cost_usd for e in used if e.estimated_cost_usd is not None]
        rows.append(AgentRunRow(
            run_id=run_id, agent_key=first.agent_key, agent_name=deployment.display_name if deployment else first.agent_key,
            organization=organization_names.get(first.organization_id),
            intelligence="advanced" if any(e.level == IntelligenceLevel.ADVANCED for e in used) else "light",
            models=sorted({e.model for e in used if e.model}), status="succeeded" if all(e.success for e in used) else "failed",
            latency_ms=sum(e.latency_ms or 0 for e in used), estimated_cost_usd=round(sum(costs), 6) if costs else None,
            requires_approval=bool(deployment and deployment.requires_approval), created_at=first.created_at,
        ))
    return rows


# --- Deploy / configure ----------------------------------------------------------------------------------------


def _apply(deployment: AgentDeployment, config: DeploymentConfig) -> None:
    fields = config.model_fields_set
    if config.status is not None:
        deployment.status = config.status
    if config.intelligence_level is not None:
        deployment.intelligence_level = IntelligenceLevel(config.intelligence_level)
    if "monthly_budget_usd" in fields:
        deployment.monthly_budget_usd = config.monthly_budget_usd
    if config.requires_approval is not None:
        deployment.requires_approval = config.requires_approval
    if config.allowed_roles is not None:
        deployment.allowed_roles = list(config.allowed_roles)


def _audit(session: Session, actor: AuthAccount, event_type: str, deployment: AgentDeployment, message: str, now: datetime) -> None:
    operations_audit.record(
        session, event_type=event_type, actor_account_id=actor.id, actor_role="admin", subject_type="agent_deployment",
        subject_id=str(deployment.id), message=message, at=now,
        metadata={"agent": deployment.agent_key, "status": deployment.status, "level": deployment.intelligence_level.value,
                  "requires_approval": deployment.requires_approval, "monthly_budget_usd": deployment.monthly_budget_usd},
    )


def deploy(session: Session, actor: AuthAccount, agent_key: str, config: DeploymentConfig, now: datetime) -> DeploymentView:
    template = TEMPLATES.get(agent_key)
    if template is None:
        detail = "is internal infrastructure and cannot be deployed" if agent_key in INTERNAL_KEYS else "is not in the agent catalog"
        raise DeploymentError("AGENT_NOT_DEPLOYABLE", f"'{agent_key}' {detail}.", 400 if agent_key in INTERNAL_KEYS else 404)
    deployment = _deployments(session).get(agent_key)
    if deployment is None:
        deployment = AgentDeployment(  # organization_id comes from the tenant session, never the client
            agent_key=agent_key, display_name=template.name, status=DEPLOYMENT_ACTIVE,
            intelligence_level=IntelligenceLevel(template.intelligence_level), requires_approval=template.requires_approval,
            allowed_roles=list(template.allowed_roles),
        )
        session.add(deployment)
    _apply(deployment, config)
    if config.status is None:
        deployment.status = DEPLOYMENT_ACTIVE
    session.flush()
    _audit(session, actor, "agent_deployed", deployment, f"{actor.display_name} deployed the {deployment.display_name} "
           f"({deployment.intelligence_level.value}, {deployment.status}).", now)
    session.commit()
    return _view(deployment, _stats(session))


def configure(session: Session, actor: AuthAccount, deployment_id: int, config: DeploymentConfig, now: datetime) -> DeploymentView:
    deployment = session.get(AgentDeployment, deployment_id)  # another organization's deployment is simply not found
    if deployment is None:
        raise DeploymentError(AGENT_NOT_DEPLOYED, "That agent deployment was not found.", 404)
    _apply(deployment, config)
    session.flush()
    _audit(session, actor, "agent_configured", deployment, f"{actor.display_name} set the {deployment.display_name} to "
           f"{deployment.status}, {deployment.intelligence_level.value}.", now)
    session.commit()
    return _view(deployment, _stats(session))


# --- Runtime gate --------------------------------------------------------------------------------------------------


def require_active(session: Session, chat_key: str, role: str) -> Optional[AgentDeployment]:
    """The deployment serving ``chat_key`` for ``role``; raises if it may not run. None = not a catalog product."""
    agent_key = CHAT_KEY_TO_AGENT.get(chat_key)
    if agent_key is None:
        return None
    deployment = _deployments(session).get(agent_key)
    name = TEMPLATES[agent_key].name
    if deployment is None:
        raise DeploymentError(AGENT_NOT_DEPLOYED, f"The {name} is not deployed for your institution.", 403)
    if deployment.status != DEPLOYMENT_ACTIVE:
        raise DeploymentError(AGENT_DISABLED, f"The {name} has been paused by your institution's administrator.", 403)
    if role not in (deployment.allowed_roles or []):
        raise DeploymentError(AGENT_ROLE_NOT_ALLOWED, f"The {name} is not enabled for your role.", 403)
    return deployment
