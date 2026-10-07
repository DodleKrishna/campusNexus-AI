"""Enterprise admin surfaces: Command Center, Workflows, Proactive Monitors, Knowledge, Connectors.

Read-only aggregations over the existing services, always in the admin's tenant
session. Nothing here invents a metric: every count comes from database rows,
and anything that is architecture/roadmap rather than implemented is labelled
so (``status`` / ``note`` fields). The request SLA shown here is a *demo
indicator* computed on read (48 h target); there is no background escalation
scheduler -- routing escalation happens when a reviewer opens an inbox.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models.academic import Exam, ExamStatus
from app.db.models.auth import AuthAccount
from app.db.models.career import Application, Opportunity, OpportunityStatus
from app.db.models.events import Event
from app.db.models.faculty import AttendanceSession, AttendanceSessionStatus
from app.db.models.mission import ApprovalRecord, Mission
from app.db.models.workflow import OperationAuditEvent, WorkflowRequest, WorkflowRequestStatus, WorkflowRequestType
from app.db.repositories import operations_audit
from app.db.session import describe_database
from app.rag.config import get_rag_config
from app.schemas.admin_console import AuditEntry, DeploymentView
from app.schemas.enums import ApprovalStatus
from app.services import admin_ops, agent_deployments, ai_usage
from app.services import department_ops as ops
from app.services.knowledge import KnowledgeService

REQUEST_SLA_HOURS = 48      # demo SLA target for a pending request
DUE_SOON_HOURS = 24
DEADLINE_WINDOW = timedelta(days=14)
_STUDENT_TYPES = (WorkflowRequestType.EVENT_PERMISSION, WorkflowRequestType.ATTENDANCE_PERMISSION,
                  WorkflowRequestType.LEAVE_REQUEST, WorkflowRequestType.OD_REQUEST)
_STAFF_TYPES = (WorkflowRequestType.FACULTY_LEAVE, WorkflowRequestType.CLASS_SUBSTITUTION, WorkflowRequestType.HOD_LEAVE,
                WorkflowRequestType.DEPARTMENT_PERMISSION, WorkflowRequestType.DEPARTMENT_RESOURCE)


class Kpi(BaseModel):
    key: str
    label: str
    value: str
    note: str


class HealthItem(BaseModel):
    key: str
    label: str
    value: int
    note: str
    link: str


class CommandCenter(BaseModel):
    kpis: List[Kpi]
    health: List[HealthItem]
    workforce: List[DeploymentView]
    activity: List[AuditEntry]


class WorkflowCard(BaseModel):
    key: str
    name: str
    trigger: str
    chain: List[str]
    sla: str
    status: str  # active / partial
    processed: int
    pending: int
    escalations: int
    note: Optional[str] = None


class MonitorFinding(BaseModel):
    title: str
    detail: str
    severity: str  # info / warning / critical


class Monitor(BaseModel):
    key: str
    name: str
    description: str
    findings_count: int
    findings: List[MonitorFinding]
    last_checked: Optional[datetime] = None
    actions: str


class KnowledgeDoc(BaseModel):
    document_id: str
    title: str
    document_type: str
    version: Optional[str] = None
    effective_from: Optional[str] = None
    agents: Dict[str, bool]


class KnowledgeHub(BaseModel):
    documents: List[KnowledgeDoc]
    indexed_chunks: int
    scope_note: str


class Connector(BaseModel):
    key: str
    name: str
    category: str
    description: str
    status: str  # connected / available / coming_next
    detail: str


def _elapsed_hours(request: WorkflowRequest, now: datetime) -> float:
    started = request.submitted_at or request.created_at
    return (now - started).total_seconds() / 3600


def sla_state(request: WorkflowRequest, now: datetime) -> str:
    if request.routing_basis in ("hod_escalation", "admin_escalation") or request.status == WorkflowRequestStatus.NEEDS_REVIEW:
        return "escalated"
    hours = _elapsed_hours(request, now)
    if hours >= REQUEST_SLA_HOURS:
        return "sla_risk"
    return "due_soon" if hours >= DUE_SOON_HOURS else "on_track"


def _open_requests(session: Session) -> List[WorkflowRequest]:
    return session.execute(select(WorkflowRequest).where(
        WorkflowRequest.status.in_((WorkflowRequestStatus.PENDING, WorkflowRequestStatus.NEEDS_REVIEW)))).scalars().all()


def _count(session: Session, model, *where) -> int:
    return session.execute(select(func.count()).select_from(model).where(*where)).scalar_one()


# --- Command Center -------------------------------------------------------------------------------------------------


def command_center(session: Session, knowledge: KnowledgeService, admin: AuthAccount, now: datetime) -> CommandCenter:
    scope = admin_ops.institution_scope(session, admin)
    usage = ai_usage.usage_summary(session)
    deployments = agent_deployments.deployments(session)
    open_requests = _open_requests(session)
    complaints = admin_ops.complaints(session, scope, now)
    active_complaints = [c for c in complaints if c.status in ("open", "in_progress")]
    breached = [c for c in active_complaints if c.response_breached or c.resolution_breached]
    request_risks = [r for r in open_requests if sla_state(r, now) in ("sla_risk", "escalated")]
    pending_approvals = sum(1 for r in open_requests if r.status == WorkflowRequestStatus.PENDING) + _count(
        session, ApprovalRecord, ApprovalRecord.status == ApprovalStatus.PENDING)
    processed = _count(session, WorkflowRequest, WorkflowRequest.status.notin_((WorkflowRequestStatus.DRAFT,))) + _count(session, Mission)
    at_risk = len({e.student_id for e in ops.attendance_insights(session, knowledge, scope, now).low_attendance})
    deadlines = _count(session, Opportunity, Opportunity.status == OpportunityStatus.OPEN, Opportunity.deadline >= now,
                       Opportunity.deadline <= now + DEADLINE_WINDOW) + _count(
        session, Event, Event.start_at >= now, Event.start_at <= now + DEADLINE_WINDOW)
    spend = usage["estimated_cost_usd"]
    kpis = [
        Kpi(key="agents", label="Active agents", value=f"{sum(1 for d in deployments if d.status == 'active')}",
            note=f"of {len(deployments)} deployed"),
        Kpi(key="workflows", label="Automated workflow runs", value=str(processed), note="requests + agent missions"),
        Kpi(key="approvals", label="Pending approvals", value=str(pending_approvals), note="requests + agent actions"),
        Kpi(key="sla", label="SLA risks", value=str(len(breached) + len(request_risks)),
            note=f"{len(breached)} cases · {len(request_risks)} requests"),
        Kpi(key="no_ai", label="No-AI resolution", value="—" if usage["no_ai_percent"] is None else f"{usage['no_ai_percent']:g}%",
            note=f"{usage['no_ai_count']} of {usage['total_requests']} requests"),
        Kpi(key="spend", label="Estimated AI spend", value="$0.00" if not spend else f"${spend:.4f}",
            note="this month · estimate" if spend else "no token-reported calls yet"),
    ]
    health = [
        HealthItem(key="attendance", label="Attendance risks", value=at_risk, note="students below the requirement", link="/admin/attendance"),
        HealthItem(key="requests", label="Open requests", value=len(open_requests), note=f"{len(request_risks)} at SLA risk", link="/admin/requests"),
        HealthItem(key="approvals", label="Pending approvals", value=pending_approvals, note="awaiting a human decision", link="/admin/requests"),
        HealthItem(key="complaints", label="Active complaints", value=len(active_complaints), note=f"{len(breached)} past SLA", link="/admin/complaints"),
        HealthItem(key="deadlines", label="Upcoming deadlines", value=deadlines, note="placements + events, next 14 days", link="/admin/monitors"),
    ]
    return CommandCenter(kpis=kpis, health=health, workforce=deployments, activity=admin_ops.audit_log(session, limit=8))


# --- Workflows ------------------------------------------------------------------------------------------------------


def _request_stats(session: Session, types, now: datetime) -> tuple:
    rows = session.execute(select(WorkflowRequest).where(WorkflowRequest.request_type.in_(types),
                                                          WorkflowRequest.status != WorkflowRequestStatus.DRAFT)).scalars().all()
    pending = [r for r in rows if r.status in (WorkflowRequestStatus.PENDING, WorkflowRequestStatus.NEEDS_REVIEW)]
    return len(rows) - len(pending), len(pending), sum(1 for r in pending if sla_state(r, now) == "escalated")


def workflows(session: Session, admin: AuthAccount, now: datetime) -> List[WorkflowCard]:
    scope = admin_ops.institution_scope(session, admin)
    student = _request_stats(session, _STUDENT_TYPES, now)
    staff = _request_stats(session, _STAFF_TYPES, now)
    complaints = admin_ops.complaints(session, scope, now)
    open_cases = [c for c in complaints if c.status in ("open", "in_progress")]
    sessions_done = _count(session, AttendanceSession, AttendanceSession.status == AttendanceSessionStatus.CLOSED)
    sessions_open = _count(session, AttendanceSession, AttendanceSession.status == AttendanceSessionStatus.ACTIVE)
    applications = _count(session, Application)
    return [
        WorkflowCard(key="student_permission", name="Student Permission", trigger="Student asks the Permission Agent",
                     chain=["Student", "Deterministic context check", "Class faculty / mentor", "HOD if unassigned", "Notification"],
                     sla=f"{REQUEST_SLA_HOURS} h review target", status="active",
                     processed=student[0], pending=student[1], escalations=student[2]),
        WorkflowCard(key="faculty_leave", name="Faculty Leave", trigger="Faculty requests leave or substitution",
                     chain=["Faculty", "Affected classes computed", "HOD", "Administration for a head's own request", "Notification"],
                     sla=f"{REQUEST_SLA_HOURS} h review target", status="active",
                     processed=staff[0], pending=staff[1], escalations=staff[2]),
        WorkflowCard(key="attendance", name="Attendance Recording & Correction", trigger="Faculty starts a class session",
                     chain=["Faculty", "Class-window rules", "Marks verified per roster", "Folded into records once", "Audit"],
                     sla="Close within the class window", status="partial", processed=sessions_done, pending=sessions_open,
                     escalations=0, note="Live recording is implemented; a correction request type is on the roadmap."),
        WorkflowCard(key="grievance", name="Grievance / Campus Case", trigger="Student files a complaint (approved action)",
                     chain=["Student", "Approval Gate", "Assigned office", "Response + resolution SLA", "Breach flagged"],
                     sla="Response and resolution due dates per case", status="active",
                     processed=len(complaints) - len(open_cases), pending=len(open_cases),
                     escalations=sum(1 for c in open_cases if c.response_breached or c.resolution_breached),
                     note="Breaches are flagged on read; automatic re-assignment is on the roadmap."),
        WorkflowCard(key="placement", name="Placement Eligibility", trigger="Student asks about an opportunity",
                     chain=["Student", "Deterministic eligibility rules", "Skill-gap guidance", "Placement cell"],
                     sla="Before the application deadline", status="active", processed=applications, pending=0, escalations=0),
    ]


# --- Proactive monitors ---------------------------------------------------------------------------------------------

MONITORS = {
    "attendance_risk": ("Attendance Risk Monitor", "Students below the attendance requirement in any course."),
    "approval_sla": ("Approval SLA Monitor", f"Pending requests past {DUE_SOON_HOURS} h, or escalated."),
    "placement_deadline": ("Placement Deadline Monitor", "Open opportunities closing in the next 14 days."),
    "exam_risk": ("Exam Risk Monitor", "At-risk attendance in a course with an exam in the next 14 days."),
    "grievance_sla": ("Grievance SLA Monitor", "Open campus cases past their response or resolution due date."),
}


def _findings(session: Session, knowledge: KnowledgeService, admin: AuthAccount, key: str, now: datetime) -> List[MonitorFinding]:
    scope = admin_ops.institution_scope(session, admin)
    if key in ("attendance_risk", "exam_risk"):
        low = ops.attendance_insights(session, knowledge, scope, now).low_attendance
        if key == "attendance_risk":
            return [MonitorFinding(title=f"{e.full_name} · {e.course_code}", severity="critical" if e.percentage < 65 else "warning",
                                   detail=f"{e.percentage:.0f}% attendance ({e.classes_attended}/{e.classes_conducted})") for e in low]
        exams = session.execute(select(Exam).where(Exam.scheduled_start >= now, Exam.scheduled_start <= now + DEADLINE_WINDOW,
                                                   or_(Exam.status.is_(None), Exam.status.not_in([ExamStatus.DRAFT, ExamStatus.CANCELLED])))
                                ).scalars().all()
        soon = {e.course.code: e for e in exams if e.course is not None}
        return [MonitorFinding(title=f"{e.full_name} · {e.course_code}", severity="critical",
                               detail=f"{e.percentage:.0f}% attendance, {soon[e.course_code].exam_type} on "
                                      f"{soon[e.course_code].scheduled_start:%d %b}") for e in low if e.course_code in soon]
    if key == "approval_sla":
        out = []
        for r in _open_requests(session):
            state = sla_state(r, now)
            if state != "on_track":
                out.append(MonitorFinding(title=f"{r.request_code} · {r.title}", severity="critical" if state != "due_soon" else "warning",
                                          detail=f"{state.replace('_', ' ')} · {_elapsed_hours(r, now):.0f} h pending"))
        return out
    if key == "placement_deadline":
        rows = session.execute(select(Opportunity).where(Opportunity.status == OpportunityStatus.OPEN, Opportunity.deadline >= now,
                                                         Opportunity.deadline <= now + DEADLINE_WINDOW).order_by(Opportunity.deadline)).scalars()
        return [MonitorFinding(title=o.title, severity="warning" if (o.deadline - now).days < 5 else "info",
                               detail=f"closes {o.deadline:%d %b} ({(o.deadline - now).days} days)") for o in rows]
    cases = [c for c in admin_ops.complaints(session, scope, now) if c.status in ("open", "in_progress")
             and (c.response_breached or c.resolution_breached)]
    return [MonitorFinding(title=f"{c.case_code} · {c.category}", severity="critical",
                           detail=f"{c.priority} priority · {c.office} · past SLA") for c in cases]


def _last_runs(session: Session) -> Dict[str, datetime]:
    rows = session.execute(select(OperationAuditEvent.subject_id, func.max(OperationAuditEvent.timestamp))
                           .where(OperationAuditEvent.event_type == "monitor_run").group_by(OperationAuditEvent.subject_id)).all()
    return {key: at for key, at in rows}


def monitors(session: Session, knowledge: KnowledgeService, admin: AuthAccount, now: datetime) -> List[Monitor]:
    last = _last_runs(session)
    out = []
    for key, (name, description) in MONITORS.items():
        findings = _findings(session, knowledge, admin, key, now)
        out.append(Monitor(key=key, name=name, description=description, findings_count=len(findings), findings=findings[:6],
                           last_checked=last.get(key), actions="Recorded in the audit trail; staff notified in their inbox views"))
    return out


def run_monitor(session: Session, knowledge: KnowledgeService, admin: AuthAccount, key: str, now: datetime) -> Monitor:
    if key not in MONITORS:
        raise KeyError(key)
    findings = _findings(session, knowledge, admin, key, now)
    operations_audit.record(session, event_type="monitor_run", actor_account_id=admin.id, actor_role="admin",
                            subject_type="monitor", subject_id=key, at=now,
                            message=f"{MONITORS[key][0]} found {len(findings)} item(s).", metadata={"findings": len(findings)})
    session.commit()
    name, description = MONITORS[key]
    return Monitor(key=key, name=name, description=description, findings_count=len(findings), findings=findings[:6],
                   last_checked=now, actions="Recorded in the audit trail; staff notified in their inbox views")


# --- Knowledge ------------------------------------------------------------------------------------------------------

# Which product agents consult each policy type (the retrieval filters the agents use).
_AGENT_ACCESS = {
    "attendance_policy": {"academic", "attendance", "enquiry"},
    "exam_regulations": {"academic", "enquiry"},
    "internship_policy": {"career", "enquiry"},
    "event_policy": {"events", "enquiry"},
    "event_circular": {"events", "enquiry"},
    "hostel_policy": {"campus_services", "enquiry"},
    "grievance_policy": {"campus_services", "enquiry"},
    "library_policy": {"campus_services", "enquiry"},
}
_AGENTS = ("academic", "attendance", "career", "events", "campus_services", "enquiry")


def _front_matter(text: str) -> Dict[str, str]:
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not match:
        return {}
    return {k.strip(): v.strip() for k, v in (line.split(":", 1) for line in match.group(1).splitlines() if ":" in line)}


def knowledge_hub(knowledge: KnowledgeService) -> KnowledgeHub:
    docs = []
    for path in sorted(get_rag_config().policy_dir.glob("*.md")):
        meta = _front_matter(path.read_text(encoding="utf-8"))
        doc_type = meta.get("document_type", path.stem)
        family = next((k for k in _AGENT_ACCESS if doc_type.startswith(k.split("_")[0]) and k in doc_type), doc_type)
        access = _AGENT_ACCESS.get(doc_type) or _AGENT_ACCESS.get(family) or {"enquiry"}
        docs.append(KnowledgeDoc(document_id=meta.get("document_id", path.stem), title=meta.get("title", path.stem),
                                 document_type=doc_type, version=meta.get("policy_version"), effective_from=meta.get("effective_from"),
                                 agents={a: a in access for a in _AGENTS}))
    try:
        chunks = knowledge.indexed_chunk_count()
    except Exception:  # noqa: BLE001 -- readiness display only
        chunks = 0
    return KnowledgeHub(documents=docs, indexed_chunks=chunks,
                        scope_note="Shared institutional corpus today; per-organization knowledge scopes are the next hardening step.")


# --- Connectors -----------------------------------------------------------------------------------------------------


def connectors(session: Session) -> List[Connector]:
    db = describe_database(session.get_bind())
    return [
        Connector(key="campusnexus_db", name="CampusNexus Database", category="Database", status="connected",
                  description="Unified campus context: students, courses, attendance, requests, cases, events.",
                  detail=f"{db['label']} · live"),
        Connector(key="postgres", name="PostgreSQL / Supabase", category="Database", status="available",
                  description="Run CampusNexus on the institution's managed PostgreSQL.", detail="Supported engine (SQLAlchemy); migration tooling included"),
        Connector(key="rest_api", name="REST API (ERP / SIS / LMS)", category="REST API", status="available",
                  description="Adapter pattern: map an existing API onto the normalized campus model.", detail="Architecture ready · adapters built per customer"),
        Connector(key="mysql", name="MySQL", category="Database", status="coming_next",
                  description="Read-only sync from legacy MySQL ERPs.", detail="Planned"),
        Connector(key="csv", name="CSV / Excel import", category="CSV / Excel", status="coming_next",
                  description="Bulk import of legacy student, course and attendance sheets.", detail="Planned"),
        Connector(key="attendance", name="Biometric attendance", category="Attendance", status="coming_next",
                  description="Pull punches from biometric / RFID attendance systems.", detail="Planned"),
        Connector(key="placement", name="Placement portal", category="Placement", status="coming_next",
                  description="Sync drives, eligibility and applications from placement portals.", detail="Planned"),
        Connector(key="communication", name="Email / Calendar / Messaging", category="Communication", status="coming_next",
                  description="Deliver notifications and write calendar entries in institutional tools.", detail="Planned (in-app notifications today)"),
    ]
