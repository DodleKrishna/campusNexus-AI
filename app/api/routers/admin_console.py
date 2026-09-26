"""/admin/* (Phase 18, JWT): the administrator console. Institution-wide scope.

Every endpoint depends on ``current_admin`` (an active ADMIN account). A
``department`` query parameter only NARROWS the institution scope to one real
department; an unknown code is a 404, never "everything".

Writes here are limited to the safe account operations (activate/deactivate,
role change within valid profile links, a one-time development password),
all audited. Audit logs are read-only. No response contains a password hash,
API key or JWT secret. Deciding requests stays on ``/requests/{id}/approve|reject``.

(The older ``GET /admin/cases`` is the Phase 8 demo-identity endpoint and is untouched.)
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.services import agent_catalog as agent_catalog_service
from app.services import agent_deployments
from app.api.ai import budget_exceeded, deployed_agent_run
from app.llm.router import AIBudgetExceededError
from app.db.tenant_session import session_organization
from app.agents.enquiry.admin import ADMIN_AGENT_KEYS, ADMIN_DISPLAY_NAMES, AdminAgent
from app.api.auth_deps import AuthenticatedUser, require_roles
from app.api.deps import get_knowledge_service, get_now, get_session
from app.auth.accounts import get_account
from app.db.models.auth import AuthAccount
from app.llm.base import LLMProviderError, LLMTransientError
from app.schemas.admin_console import (
    CatalogEntry,
    DeploymentConfig,
    DeploymentView,
    AIBudgetBody,
    AIUsageSummary,
    AgentCatalogView,
    ActiveBody,
    AdminAttendance,
    AdminComplaint,
    AdminDashboard,
    AIOperations,
    AuditEntry,
    DepartmentDetail,
    DepartmentRow,
    PasswordReset,
    RoleChangeBody,
    SystemStatus,
    UserView,
)
from app.schemas.agent_chat import AgentQueryRequest, AgentQueryResponse
from app.schemas.department import StaffNotificationItem
from app.schemas.enums import UserRole
from app.services import account_notifications, admin_ops
from app.services.admin_ops import AdminError
from app.services.knowledge import KnowledgeService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin console"])

_AGENT_DESCRIPTIONS = {
    "enquiry": "Institution-wide questions answered by checking operations, attendance, requests, complaints and system health. Read-only.",
    "academic": "Classes running or not started, and attendance risk across departments.",
    "complaints": "Complaints across the institution and their SLA state.",
    "permission": "Requests pending anywhere, and escalations to HODs and the administration.",
    "events": "Upcoming campus events.",
}


def current_admin(user: AuthenticatedUser = Depends(require_roles(UserRole.ADMIN)), session: Session = Depends(get_session)) -> AuthAccount:
    account = get_account(session, user.account_id)
    if account is None or not account.is_active:
        raise HTTPException(status_code=403, detail="Your role does not have access to this.")
    return account


def _run(operation):
    try:
        return operation()
    except AdminError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


class AgentItem(BaseModel):
    key: str
    display_name: str
    description: str


class AgentCatalog(BaseModel):
    live_ai: bool
    provider: str
    model: Optional[str]
    agents: List[AgentItem]


@router.get("/dashboard", response_model=AdminDashboard)
def dashboard(
    department: Optional[str] = Query(default=None, max_length=20), admin: AuthAccount = Depends(current_admin),
    session: Session = Depends(get_session), now: datetime = Depends(get_now),
) -> AdminDashboard:
    scope = _run(lambda: admin_ops.institution_scope(session, admin, department))
    return admin_ops.dashboard(session, scope, now, department)


@router.get("/departments", response_model=List[DepartmentRow])
def departments(
    admin: AuthAccount = Depends(current_admin), session: Session = Depends(get_session),
    knowledge: KnowledgeService = Depends(get_knowledge_service), now: datetime = Depends(get_now),
) -> List[DepartmentRow]:
    return admin_ops.departments(session, knowledge, admin_ops.institution_scope(session, admin), now)


@router.get("/departments/{code}", response_model=DepartmentDetail)
def department_detail(
    code: str, admin: AuthAccount = Depends(current_admin), session: Session = Depends(get_session),
    knowledge: KnowledgeService = Depends(get_knowledge_service), now: datetime = Depends(get_now),
) -> DepartmentDetail:
    return _run(lambda: admin_ops.department_detail(session, knowledge, admin, code, now))


@router.get("/attendance", response_model=AdminAttendance)
def attendance(
    department: Optional[str] = Query(default=None, max_length=20), admin: AuthAccount = Depends(current_admin),
    session: Session = Depends(get_session), knowledge: KnowledgeService = Depends(get_knowledge_service),
    now: datetime = Depends(get_now),
) -> AdminAttendance:
    scope = _run(lambda: admin_ops.institution_scope(session, admin, department))
    return admin_ops.attendance(session, knowledge, scope, now)


@router.get("/complaints", response_model=List[AdminComplaint])
def complaints(
    department: Optional[str] = Query(default=None, max_length=20),
    status: Optional[Literal["open", "in_progress", "resolved", "closed"]] = None,
    priority: Optional[Literal["low", "normal", "high", "urgent"]] = None,
    breached: bool = False, admin: AuthAccount = Depends(current_admin), session: Session = Depends(get_session),
    now: datetime = Depends(get_now),
) -> List[AdminComplaint]:
    scope = _run(lambda: admin_ops.institution_scope(session, admin, department))
    return admin_ops.complaints(session, scope, now, status=status, priority=priority, breached_only=breached)


@router.get("/users", response_model=List[UserView])
def users(_: AuthAccount = Depends(current_admin), session: Session = Depends(get_session)) -> List[UserView]:
    return admin_ops.users(session)


@router.post("/users/{account_id}/active", response_model=UserView)
def set_active(
    account_id: int, body: ActiveBody, admin: AuthAccount = Depends(current_admin), session: Session = Depends(get_session),
    now: datetime = Depends(get_now),
) -> UserView:
    return _run(lambda: admin_ops.set_active(session, admin, account_id, body.is_active, now))


@router.post("/users/{account_id}/role", response_model=UserView)
def change_role(
    account_id: int, body: RoleChangeBody, admin: AuthAccount = Depends(current_admin), session: Session = Depends(get_session),
    now: datetime = Depends(get_now),
) -> UserView:
    return _run(lambda: admin_ops.change_role(session, admin, account_id, body.role, now))


@router.post("/users/{account_id}/reset-password", response_model=PasswordReset)
def reset_password(
    account_id: int, admin: AuthAccount = Depends(current_admin), session: Session = Depends(get_session),
    now: datetime = Depends(get_now),
) -> PasswordReset:
    return _run(lambda: admin_ops.reset_password(session, admin, account_id, now))


@router.get("/ai-operations", response_model=AIOperations)
def ai_operations(
    request: Request, _: AuthAccount = Depends(current_admin), session: Session = Depends(get_session),
    knowledge: KnowledgeService = Depends(get_knowledge_service),
) -> AIOperations:
    return admin_ops.ai_operations(session, request.app.state.llm_provider, knowledge)


@router.put("/ai-budget", response_model=AIUsageSummary)
def set_ai_budget(
    body: AIBudgetBody, admin: AuthAccount = Depends(current_admin), session: Session = Depends(get_session),
    now: datetime = Depends(get_now),
) -> AIUsageSummary:
    """The admin's own organization's monthly AI budget (from the token's organization; null = no limit)."""
    return admin_ops.set_ai_budget(session, admin, body.monthly_budget_usd, now)


@router.get("/agent-catalog", response_model=AgentCatalogView)
def agent_catalog(_: AuthAccount = Depends(current_admin)) -> AgentCatalogView:
    return agent_catalog_service.catalog()


# --- Agent-as-a-Product: catalog -> configure -> deploy (organization from the token, never the client) -------


def _deployment_call(operation):
    try:
        return operation()
    except agent_deployments.DeploymentError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@router.get("/agents/catalog", response_model=List[CatalogEntry])
def agents_catalog(_: AuthAccount = Depends(current_admin), session: Session = Depends(get_session)) -> List[CatalogEntry]:
    return agent_deployments.catalog(session)


@router.get("/agents/deployments", response_model=List[DeploymentView])
def agents_deployments(_: AuthAccount = Depends(current_admin), session: Session = Depends(get_session)) -> List[DeploymentView]:
    return agent_deployments.deployments(session)


@router.post("/agents/{agent_key}/deploy", response_model=DeploymentView)
def deploy_agent(
    agent_key: str, body: DeploymentConfig, admin: AuthAccount = Depends(current_admin), session: Session = Depends(get_session),
    now: datetime = Depends(get_now),
) -> DeploymentView:
    return _deployment_call(lambda: agent_deployments.deploy(session, admin, agent_key, body, now))


@router.patch("/agents/deployments/{deployment_id}", response_model=DeploymentView)
def configure_agent(
    deployment_id: int, body: DeploymentConfig, admin: AuthAccount = Depends(current_admin),
    session: Session = Depends(get_session), now: datetime = Depends(get_now),
) -> DeploymentView:
    return _deployment_call(lambda: agent_deployments.configure(session, admin, deployment_id, body, now))


@router.get("/audit", response_model=List[AuditEntry])
def audit(
    source: Optional[Literal["mission", "operations"]] = None, action: Optional[str] = Query(default=None, max_length=60),
    limit: int = Query(default=200, ge=1, le=500), _: AuthAccount = Depends(current_admin), session: Session = Depends(get_session),
) -> List[AuditEntry]:
    return admin_ops.audit_log(session, source=source, action=action, limit=limit)


@router.get("/system", response_model=SystemStatus)
def system(
    request: Request, _: AuthAccount = Depends(current_admin), session: Session = Depends(get_session),
    knowledge: KnowledgeService = Depends(get_knowledge_service),
) -> SystemStatus:
    return admin_ops.system_status(session, request.app.state.llm_provider, knowledge)


@router.get("/notifications", response_model=List[StaffNotificationItem])
def notifications(admin: AuthAccount = Depends(current_admin), session: Session = Depends(get_session)) -> List[StaffNotificationItem]:
    return account_notifications.list_for(session, admin.id)


@router.get("/agents", response_model=AgentCatalog)
def agent_catalog(request: Request, _: AuthAccount = Depends(current_admin)) -> AgentCatalog:
    provider = request.app.state.llm_provider
    return AgentCatalog(
        live_ai=bool(provider.is_live), provider=provider.name, model=provider.model_name,
        agents=[AgentItem(key=k, display_name=ADMIN_DISPLAY_NAMES[k], description=_AGENT_DESCRIPTIONS[k]) for k in ADMIN_AGENT_KEYS],
    )


@router.post("/agents/{agent_key}/query", response_model=AgentQueryResponse)
def agent_query(
    agent_key: str, body: AgentQueryRequest, request: Request, admin: AuthAccount = Depends(current_admin),
    session: Session = Depends(get_session), knowledge: KnowledgeService = Depends(get_knowledge_service),
    now: datetime = Depends(get_now),
) -> AgentQueryResponse:
    if agent_key not in ADMIN_AGENT_KEYS:
        raise HTTPException(status_code=404, detail=f"There is no '{agent_key}' agent available to chat with.")
    agent = AdminAgent(llm_provider=request.app.state.llm_provider, knowledge=knowledge)
    try:
        with deployed_agent_run(request, session, agent_key, "admin", session_organization(session)):
            return agent.handle(session, admin, agent_key, body.message, now)
    except AIBudgetExceededError as exc:
        raise budget_exceeded(exc) from exc
    except LLMTransientError as exc:
        logger.warning("admin chat: provider unavailable (%s)", exc.details())
        raise HTTPException(status_code=503, detail="Live AI is temporarily unavailable. Please try again shortly.") from exc
    except LLMProviderError as exc:
        logger.error("admin chat: provider error: %s", exc)
        raise HTTPException(status_code=502, detail="The AI provider returned an unusable response. Please try again.") from exc
