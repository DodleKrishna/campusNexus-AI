"""Institution operations for administrators (Phase 18). Read-mostly, deterministic.

Scope: an ADMIN account sees every department (``institution_scope``). The
class, attendance, risk and complaint figures reuse ``department_ops`` with an
``InstitutionScope`` -- one implementation for HOD and admin, never a second
copy of the class-state or attendance logic.

The only writes here are the safe account operations (activate/deactivate,
role change when the profile links stay valid, a one-time development
password), each audited in ``operation_audit_events``. Audit sources are
read-only: nothing here updates or deletes a log row. Nothing returned here
ever contains a password hash, API key or JWT secret.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.models.organization import Organization
from app.db.tenant_session import session_organization
from app.llm.router import ADVANCED_MODEL, LIGHT_MODEL
from app.services import ai_usage
from app.auth.passwords import hash_password
from app.db.models.auth import AuthAccount
from app.db.models.organization import OrganizationMembership
from app.db.session import describe_database
from app.db.models.faculty import AttendanceSession, AttendanceSessionStatus, FacultyProfile, TeachingAssignment
from app.db.models.identity import Department, Student
from app.db.models.mission import AgentRun, ApprovalRecord, AuditLog, Mission
from app.db.models.workflow import OperationAuditEvent, WorkflowRequest, WorkflowRequestStatus
from app.db.repositories import operations_audit
from app.db.tenancy import set_account_role
from app.rules.class_session import ClassState
from app.schemas.admin_console import (
    AIUsageSummary,
    ControlTowerMission,
    AdminAttendance,
    AdminComplaint,
    AdminDashboard,
    AIOperations,
    AuditEntry,
    DepartmentAttendance,
    DepartmentDetail,
    DepartmentRow,
    MissionSummary,
    OpsEvent,
    PasswordReset,
    SystemComponent,
    SystemStatus,
    UserView,
)
from app.schemas.enums import AgentResultStatus, ApprovalStatus, MissionStatus, UserRole
from app.services import department_ops as ops
from app.services.class_schedule import local
from app.services.knowledge import KnowledgeService

ERROR_EVENTS = ("provider_unavailable", "plan_generation_failed", "plan_invalid", "task_failed", "duplicate_failure_detected")
WORKFLOW_FAILURE_EVENTS = ("plan_generation_failed", "plan_invalid", "task_failed", "duplicate_failure_detected")


class AdminError(Exception):
    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def institution_scope(session: Session, account: AuthAccount, department_code: Optional[str] = None) -> ops.InstitutionScope:
    """Every department, or the one named by ``department_code`` (an unknown code is a 404, never 'all')."""
    query = select(Department).order_by(Department.code)
    if department_code:
        query = query.where(func.upper(Department.code) == department_code.strip().upper())
    departments = tuple(session.execute(query).scalars().all())
    if department_code and not departments:
        raise AdminError(f"There is no department '{department_code}'.", status_code=404)
    return ops.InstitutionScope(account=account, departments=departments)


def _count(session: Session, model, *where) -> int:
    return session.execute(select(func.count()).select_from(model).where(*where)).scalar_one()


def _breached(c) -> bool:
    return (c.response_breached or c.resolution_breached) and c.status in ("open", "in_progress")


# ---------------------------------------------------------------------------
# Dashboard and departments
# ---------------------------------------------------------------------------


def system_errors_since(session: Session, since: datetime) -> int:
    events = _count(session, AuditLog, AuditLog.event_type.in_(ERROR_EVENTS), AuditLog.timestamp >= since)
    return events + _count(session, Mission, Mission.status == MissionStatus.FAILED, Mission.updated_at >= since)


def escalations(session: Session, scope: ops.InstitutionScope) -> int:
    return _count(
        session, WorkflowRequest, WorkflowRequest.department_id.in_(scope.department_ids),
        WorkflowRequest.submitted_at.is_not(None),
        ((WorkflowRequest.status == WorkflowRequestStatus.PENDING)
         & WorkflowRequest.routing_basis.in_(("hod_escalation", "admin_escalation")))
        | (WorkflowRequest.status == WorkflowRequestStatus.NEEDS_REVIEW),
    )


def dashboard(session: Session, scope: ops.InstitutionScope, now: datetime, department_filter: Optional[str] = None) -> AdminDashboard:
    views = ops.activity(session, scope, now)
    states = [v.state for v in views]
    complaints = ops.complaints(session, scope, now)
    return AdminDashboard(
        date=local(now).date(), now_local=local(now).strftime("%H:%M"), start_grace_minutes=ops.start_grace_minutes(),
        total_students=len(ops.students(session, scope)), total_faculty=len(ops.faculty_members(session, scope)),
        departments=len(scope.departments), classes_today=sum(1 for s in states if s != ClassState.CANCELLED.value),
        active_classes=states.count(ClassState.ACTIVE.value),
        not_started_classes=sum(1 for s in states if s in (ClassState.DELAYED.value, ClassState.NOT_HELD.value)),
        completed_classes=states.count(ClassState.COMPLETED.value), cancelled_classes=states.count(ClassState.CANCELLED.value),
        pending_requests=_count(session, WorkflowRequest, WorkflowRequest.department_id.in_(scope.department_ids),
                                WorkflowRequest.status == WorkflowRequestStatus.PENDING),
        escalations=escalations(session, scope), sla_breaches=sum(1 for c in complaints if _breached(c)),
        system_errors_24h=system_errors_since(session, now - timedelta(hours=24)), activity=views,
        department_filter=department_filter,
    )


def department_row(session: Session, knowledge: KnowledgeService, account: AuthAccount, department: Department, now: datetime) -> DepartmentRow:
    scope = ops.InstitutionScope(account=account, departments=(department,))
    views = ops.activity(session, scope, now)
    risk = ops.student_risk(session, knowledge, scope, now)
    complaints = ops.complaints(session, scope, now)
    head = session.get(FacultyProfile, department.hod_faculty_id) if department.hod_faculty_id else None
    return DepartmentRow(
        department_id=department.id, code=department.code, name=department.name, hod_name=head.full_name if head else None,
        faculty_count=len(ops.faculty_members(session, scope)), student_count=len(risk),
        classes_today=sum(1 for v in views if v.state != ClassState.CANCELLED.value),
        active_classes=sum(1 for v in views if v.state == ClassState.ACTIVE.value), not_started_classes=len(ops.not_started(views)),
        attendance_risk_students=sum(1 for r in risk if r.courses_below_threshold),
        pending_requests=_count(session, WorkflowRequest, WorkflowRequest.department_id == department.id,
                                WorkflowRequest.status == WorkflowRequestStatus.PENDING),
        open_complaints=sum(1 for c in complaints if c.status in ("open", "in_progress")),
        sla_breaches=sum(1 for c in complaints if _breached(c)),
    )


def departments(session: Session, knowledge: KnowledgeService, scope: ops.InstitutionScope, now: datetime) -> List[DepartmentRow]:
    return [department_row(session, knowledge, scope.account, d, now) for d in scope.departments]


def department_detail(session: Session, knowledge: KnowledgeService, account: AuthAccount, code: str, now: datetime) -> DepartmentDetail:
    scope = institution_scope(session, account, code)
    department = scope.departments[0]
    return DepartmentDetail(
        department=department_row(session, knowledge, account, department, now), activity=ops.activity(session, scope, now),
        faculty=ops.faculty_list(session, scope, now),
        at_risk_students=[r for r in ops.student_risk(session, knowledge, scope, now) if r.courses_below_threshold],
        attendance=ops.attendance_insights(session, knowledge, scope, now), complaints=ops.complaints(session, scope, now),
    )


# ---------------------------------------------------------------------------
# Attendance and complaints
# ---------------------------------------------------------------------------


def attendance(session: Session, knowledge: KnowledgeService, scope: ops.InstitutionScope, now: datetime) -> AdminAttendance:
    insights = ops.attendance_insights(session, knowledge, scope, now)
    by_department: List[DepartmentAttendance] = []
    for d in scope.departments:
        one = ops.InstitutionScope(account=scope.account, departments=(d,))
        risk = ops.student_risk(session, knowledge, one, now)
        attended = sum(r.classes_attended for r in risk)
        conducted = sum(r.classes_conducted for r in risk)
        by_department.append(DepartmentAttendance(
            code=d.code, name=d.name, students=len(risk), classes_attended=attended, classes_conducted=conducted,
            percentage=ops._pct(attended, conducted), students_below_threshold=sum(1 for r in risk if r.courses_below_threshold),
        ))
    rows = session.execute(
        select(AttendanceSession).join(TeachingAssignment, TeachingAssignment.id == AttendanceSession.teaching_assignment_id)
        .where(AttendanceSession.status == AttendanceSessionStatus.ACTIVE, TeachingAssignment.department_id.in_(scope.department_ids))
        .order_by(AttendanceSession.actual_started_at.desc())
    ).scalars().all()
    active = [ops._session_summary(session, r) for r in rows]
    return AdminAttendance(
        required_percentage=insights.required_percentage, by_department=by_department, insights=insights, active_sessions=active,
        low_attendance_students=len({e.student_id for e in insights.low_attendance}),
    )


def complaints(
    session: Session, scope: ops.InstitutionScope, now: datetime, *, status: Optional[str] = None,
    priority: Optional[str] = None, breached_only: bool = False,
) -> List[AdminComplaint]:
    codes = {s.student_code: s.department.code for s in ops.students(session, scope)}
    result = []
    for c in ops.complaints(session, scope, now):
        if status and c.status != status:
            continue
        if priority and c.priority != priority:
            continue
        if breached_only and not (c.response_breached or c.resolution_breached):
            continue
        result.append(AdminComplaint(**c.model_dump(), student_department=codes.get(c.student_id, "")))
    return result


# ---------------------------------------------------------------------------
# Users and roles (safe operations only)
# ---------------------------------------------------------------------------


def _is_head(session: Session, account: AuthAccount) -> bool:
    if account.linked_faculty_id is None:
        return False
    faculty = session.get(FacultyProfile, account.linked_faculty_id)
    return bool(faculty and faculty.department and faculty.department.hod_faculty_id == faculty.id)


def allowed_roles(session: Session, account: AuthAccount) -> List[str]:
    """Roles this account can hold without breaking its profile links."""
    if account.linked_student_id:
        return [UserRole.STUDENT.value]
    if account.linked_faculty_id:
        roles = [UserRole.FACULTY.value]
        if _is_head(session, account):
            roles.append(UserRole.HOD.value)
        return roles
    return [UserRole.ADMIN.value, UserRole.STAFF.value]


def user_view(session: Session, account: AuthAccount) -> UserView:
    faculty = session.get(FacultyProfile, account.linked_faculty_id) if account.linked_faculty_id else None
    student = session.execute(select(Student).where(Student.student_code == account.linked_student_id)).scalar_one_or_none() if account.linked_student_id else None
    department = (faculty.department if faculty else student.department if student else
                  session.get(Department, account.department_id) if account.department_id else None)
    return UserView(
        account_id=account.id, email=account.email, display_name=account.display_name, role=account.role.value,
        is_active=account.is_active, department_code=department.code if department else None,
        linked_student_id=account.linked_student_id,
        linked_faculty=f"{faculty.employee_code} · {faculty.full_name}" if faculty else None,
        is_department_head=_is_head(session, account), last_login_at=account.last_login_at,
        allowed_roles=allowed_roles(session, account),
    )


def users(session: Session) -> List[UserView]:
    # Phase 22C: accounts are global; only those with a membership in this (tenant-scoped) organization are listed.
    members = select(AuthAccount).join(OrganizationMembership, OrganizationMembership.account_id == AuthAccount.id)
    return [user_view(session, a) for a in session.execute(members.order_by(AuthAccount.role, AuthAccount.email)).scalars()]


def _target(session: Session, actor: AuthAccount, account_id: int) -> AuthAccount:
    # Phase 22C: an account of another organization is "not found" (its membership is filtered out).
    account = session.execute(
        select(AuthAccount).join(OrganizationMembership, OrganizationMembership.account_id == AuthAccount.id)
        .where(AuthAccount.id == account_id)
    ).scalar_one_or_none()
    if account is None:
        raise AdminError("That account was not found.", status_code=404)
    if account.id == actor.id:
        raise AdminError("You cannot change your own account here.", status_code=403)
    return account


def _audit(session: Session, actor: AuthAccount, event_type: str, account: AuthAccount, message: str, now: datetime, **metadata) -> None:
    operations_audit.record(
        session, event_type=event_type, actor_account_id=actor.id, actor_role=actor.role.value, subject_type="auth_account",
        subject_id=str(account.id), message=message, metadata=metadata, at=now,
    )


def set_active(session: Session, actor: AuthAccount, account_id: int, is_active: bool, now: datetime) -> UserView:
    account = _target(session, actor, account_id)
    if account.is_active != is_active:
        account.is_active = is_active
        _audit(session, actor, "account_activated" if is_active else "account_deactivated", account,
               f"{actor.display_name} {'activated' if is_active else 'deactivated'} {account.email}.", now)
        session.commit()
    return user_view(session, account)


def change_role(session: Session, actor: AuthAccount, account_id: int, role: str, now: datetime) -> UserView:
    account = _target(session, actor, account_id)
    permitted = allowed_roles(session, account)
    if role not in permitted:
        raise AdminError(f"{account.email} cannot hold the role '{role}' with its current profile links (allowed: {', '.join(permitted)}).")
    if account.role.value != role:
        previous = account.role.value
        set_account_role(session, account, UserRole(role))  # membership (authoritative) + account mirror
        _audit(session, actor, "account_role_changed", account, f"{actor.display_name} changed {account.email} from {previous} to {role}.",
               now, previous=previous, role=role)
        session.commit()
    return user_view(session, account)


def reset_password(session: Session, actor: AuthAccount, account_id: int, now: datetime) -> PasswordReset:
    """Development only: a random one-time password, shown once, never stored in clear or logged."""
    account = _target(session, actor, account_id)
    password = secrets.token_urlsafe(12)
    account.password_hash = hash_password(password)
    _audit(session, actor, "account_password_reset", account, f"{actor.display_name} reset the password of {account.email}.", now)
    session.commit()
    return PasswordReset(account_id=account.id, temporary_password=password,
                         note="Shown once. Give it to the user; it is not stored anywhere in clear text.")


# ---------------------------------------------------------------------------
# AI operations, audit, system
# ---------------------------------------------------------------------------


def _ops_event(row: AuditLog) -> OpsEvent:
    return OpsEvent(timestamp=row.timestamp, event_type=row.event_type, reference=row.mission_id, message=row.message,
                    details={k: v for k, v in (row.event_metadata or {}).items() if k in ("kind", "provider", "model", "status_code", "retries", "task_id", "reasons")})


def ai_operations(session: Session, provider, knowledge: KnowledgeService) -> AIOperations:
    counts = dict(session.execute(select(Mission.status, func.count()).group_by(Mission.status)).all())
    provider_rows = session.execute(select(AuditLog).where(AuditLog.event_type == "provider_unavailable").order_by(AuditLog.id.desc())).scalars().all()
    rate_limits = sum(1 for r in provider_rows if (r.event_metadata or {}).get("kind") == "rate_limit" or (r.event_metadata or {}).get("status_code") == 429)
    failure_rows = session.execute(select(AuditLog).where(AuditLog.event_type.in_(WORKFLOW_FAILURE_EVENTS)).order_by(AuditLog.id.desc()).limit(10)).scalars().all()
    recent = session.execute(select(Mission).order_by(Mission.created_at.desc()).limit(10)).scalars().all()
    try:
        chunks = knowledge.indexed_chunk_count()
    except Exception:  # noqa: BLE001 -- readiness report, never a crash
        chunks = 0
    try:
        session.execute(text("SELECT 1"))
        database_ready = True
    except Exception:  # noqa: BLE001
        database_ready = False
    return AIOperations(
        provider=provider.name, model=provider.model_name, live=bool(provider.is_live),
        live_ai_configured={"groq": bool(os.environ.get("GROQ_API_KEY")), "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY"))},
        missions_by_status={(k.value if hasattr(k, "value") else str(k)): v for k, v in counts.items()},
        recent_missions=[MissionSummary(mission_id=m.mission_id, user_id=m.user_id, goal=m.original_goal[:160], status=m.status.value,
                                        created_at=m.created_at, updated_at=m.updated_at) for m in recent],
        agent_runs_total=_count(session, AgentRun), agent_runs_failed=_count(session, AgentRun, AgentRun.status == AgentResultStatus.FAILED),
        failed_missions=counts.get(MissionStatus.FAILED, 0), provider_errors=len(provider_rows), rate_limit_incidents=rate_limits,
        recent_provider_errors=[_ops_event(r) for r in provider_rows[:10]],
        pending_approvals=_count(session, ApprovalRecord, ApprovalRecord.status == ApprovalStatus.PENDING),
        stale_approvals=_count(session, ApprovalRecord, ApprovalRecord.status == ApprovalStatus.STALE),
        workflow_failures=_count(session, AuditLog, AuditLog.event_type.in_(WORKFLOW_FAILURE_EVENTS)),
        recent_workflow_failures=[_ops_event(r) for r in failure_rows],
        requests_needing_review=_count(session, WorkflowRequest, WorkflowRequest.status == WorkflowRequestStatus.NEEDS_REVIEW),
        rag_chunks=chunks, rag_ready=chunks > 0, database_ready=database_ready,
        routing={"no_ai": "deterministic rules / workflow", "light": LIGHT_MODEL, "advanced": ADVANCED_MODEL},
        usage=AIUsageSummary(**ai_usage.usage_summary(session)),
        control_tower=[ControlTowerMission(**row) for row in ai_usage.control_tower(session)],
    )


def audit_log(
    session: Session, *, source: Optional[str] = None, action: Optional[str] = None, limit: int = 200,
) -> List[AuditEntry]:
    """Mission audit (``audit_logs``) and operations audit (``operation_audit_events``), newest first. Read-only."""
    entries: List[AuditEntry] = []
    accounts: Dict[int, AuthAccount] = {a.id: a for a in session.execute(select(AuthAccount)).scalars()}
    if source in (None, "mission"):
        query = select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit)
        if action:
            query = query.where(AuditLog.event_type.icontains(action, autoescape=True))
        for row in session.execute(query).scalars():
            entries.append(AuditEntry(
                timestamp=row.timestamp, source="mission", actor=row.actor, role="agent" if row.actor.endswith("agent") or row.actor.startswith("mission") else "user",
                action=row.event_type, target=f"step {row.step_id}" if row.step_id else "mission", reference=row.mission_id,
                outcome=row.message,
            ))
    if source in (None, "operations"):
        query = select(OperationAuditEvent).order_by(OperationAuditEvent.timestamp.desc()).limit(limit)
        if action:
            query = query.where(OperationAuditEvent.event_type.icontains(action, autoescape=True))
        for row in session.execute(query).scalars():
            actor = accounts.get(row.actor_account_id) if row.actor_account_id else None
            entries.append(AuditEntry(
                timestamp=row.timestamp, source="operations", actor=actor.display_name if actor else "system", role=row.actor_role,
                action=row.event_type, target=row.subject_type, reference=row.subject_id, outcome=row.message,
            ))
    entries.sort(key=lambda e: e.timestamp, reverse=True)
    return entries[:limit]


def _db_mode(session: Optional[Session] = None) -> str:
    """Which database the API runs on, never its URL, host or credentials (Phase 21)."""
    if session is not None and session.get_bind().dialect.name == "postgresql":
        return describe_database(session.get_bind())["label"]
    raw = os.environ.get("CAMPUSNEXUS_DB_PATH", "")
    if not raw:
        return "SQLite -- development (data/campusnexus.db)"
    name = Path(raw).name
    return f"SQLite -- demo ({name})" if "demo" in raw.replace("\\", "/") else f"SQLite -- custom ({name})"


def system_status(session: Session, provider, knowledge: KnowledgeService) -> SystemStatus:
    """Readiness plus a safe configuration summary. Never a key, secret or hash -- only whether one is set."""
    components: List[SystemComponent] = []
    try:
        students = _count(session, Student)
        components.append(SystemComponent(name="Database", status="ready" if students else "degraded",
                                          detail=f"{students} students; {_db_mode(session)}" if students else "Connected but not seeded"))
    except Exception as exc:  # noqa: BLE001
        components.append(SystemComponent(name="Database", status="unavailable", detail=type(exc).__name__))
    try:
        chunks = knowledge.indexed_chunk_count()
    except Exception:  # noqa: BLE001
        chunks = 0
    components.append(SystemComponent(name="RAG / policy store", status="ready" if chunks else "unavailable",
                                      detail=f"{chunks} policy chunks indexed" if chunks else "No policy chunks indexed"))
    live = bool(provider.is_live)
    configured = {"groq": bool(os.environ.get("GROQ_API_KEY")), "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY"))}
    if live:
        llm_status, llm_detail = "ready", f"{provider.name} ({provider.model_name}) -- live"
    else:
        llm_status = "degraded"
        llm_detail = "Mock provider (deterministic, not live AI)" + (
            f"; a key is configured for {', '.join(k for k, v in configured.items() if v)} but not selected" if any(configured.values()) else "")
    components.append(SystemComponent(name="LLM provider", status=llm_status, detail=llm_detail))
    overall = "ready" if all(c.status == "ready" for c in components) else "degraded"
    return SystemStatus(
        overall=overall, components=components,
        settings={
            "llm_provider": provider.name, "llm_model": provider.model_name, "llm_live": live,
            "live_ai_key_configured": configured, "class_start_grace_minutes": ops.start_grace_minutes(),
            "embedding_provider": os.environ.get("CAMPUSNEXUS_EMBEDDING_PROVIDER") or "onnx_minilm (default)",
            "database_mode": _db_mode(session),
            "vector_store_path": os.environ.get("CAMPUSNEXUS_VECTOR_STORE_PATH") or "data/chroma (default)",
            "jwt_secret_configured": bool(os.environ.get("CAMPUSNEXUS_JWT_SECRET")),
            "jwt_ttl_minutes": os.environ.get("CAMPUSNEXUS_JWT_TTL_MINUTES") or "default",
        },
    )


def pending_everywhere(session: Session, scope: ops.InstitutionScope) -> List[WorkflowRequest]:
    return list(session.execute(select(WorkflowRequest).where(
        WorkflowRequest.department_id.in_(scope.department_ids),
        WorkflowRequest.status.in_([WorkflowRequestStatus.PENDING, WorkflowRequestStatus.NEEDS_REVIEW]),
    ).order_by(WorkflowRequest.submitted_at)).scalars().all())


def set_ai_budget(session: Session, actor: AuthAccount, monthly_budget_usd: Optional[float], now: datetime) -> AIUsageSummary:
    """Set this organization's monthly AI budget (None = no limit). The organization is the session's, never the client's."""
    organization = session.get(Organization, session_organization(session))
    previous = organization.monthly_ai_budget_usd
    organization.monthly_ai_budget_usd = monthly_budget_usd
    operations_audit.record(
        session, event_type="ai_budget_changed", actor_account_id=actor.id, actor_role="admin", subject_type="organization",
        subject_id=str(organization.id), message=f"{actor.display_name} set the monthly AI budget to "
        f"{'no limit' if monthly_budget_usd is None else f'${monthly_budget_usd:.2f}'}.",
        metadata={"previous": previous, "budget": monthly_budget_usd}, at=now,
    )
    session.commit()
    return AIUsageSummary(**ai_usage.usage_summary(session))
