"""Phase 18: administrator console views and the admin query plan (LLM structured output)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.department import (
    AttendanceInsights,
    DepartmentClassView,
    DepartmentComplaint,
    FacultySummary,
    SessionSummary,
    StudentRisk,
)


class DepartmentRow(BaseModel):
    department_id: int
    code: str
    name: str
    hod_name: Optional[str] = None
    faculty_count: int
    student_count: int
    classes_today: int
    active_classes: int
    not_started_classes: int
    attendance_risk_students: int
    pending_requests: int
    open_complaints: int
    sla_breaches: int


class AdminDashboard(BaseModel):
    date: date
    now_local: str
    start_grace_minutes: int
    total_students: int
    total_faculty: int
    departments: int
    classes_today: int
    active_classes: int
    not_started_classes: int
    completed_classes: int
    cancelled_classes: int
    pending_requests: int
    escalations: int
    sla_breaches: int
    system_errors_24h: int
    activity: List[DepartmentClassView]
    department_filter: Optional[str] = None


class DepartmentDetail(BaseModel):
    department: DepartmentRow
    activity: List[DepartmentClassView]
    faculty: List[FacultySummary]
    at_risk_students: List[StudentRisk]
    attendance: AttendanceInsights
    complaints: List[DepartmentComplaint]


class DepartmentAttendance(BaseModel):
    code: str
    name: str
    students: int
    classes_attended: int
    classes_conducted: int
    percentage: Optional[float] = None
    students_below_threshold: int


class AdminAttendance(BaseModel):
    required_percentage: Optional[float] = None
    by_department: List[DepartmentAttendance]
    insights: AttendanceInsights
    active_sessions: List[SessionSummary]
    low_attendance_students: int


class AdminComplaint(DepartmentComplaint):
    student_department: str


class UserView(BaseModel):
    account_id: int
    email: str
    display_name: str
    role: str
    is_active: bool
    department_code: Optional[str] = None
    linked_student_id: Optional[str] = None
    linked_faculty: Optional[str] = None  # "EMP-CSE-004 · Dr. Ashok Verma"
    is_department_head: bool = False
    last_login_at: Optional[datetime] = None
    allowed_roles: List[str] = Field(default_factory=list)


class RoleChangeBody(BaseModel):
    role: Literal["student", "faculty", "hod", "admin", "staff"]


class ActiveBody(BaseModel):
    is_active: bool


class PasswordReset(BaseModel):
    account_id: int
    temporary_password: str
    note: str


class MissionSummary(BaseModel):
    mission_id: str
    user_id: str
    goal: str
    status: str
    created_at: datetime
    updated_at: datetime


class OpsEvent(BaseModel):
    timestamp: datetime
    event_type: str
    reference: str
    message: str
    details: Dict[str, Any] = Field(default_factory=dict)


class AIUsageSummary(BaseModel):
    """Hackathon AI telemetry for the caller's organization. None = unavailable (never invented)."""
    total_requests: int = 0
    no_ai_count: int = 0
    no_ai_percent: Optional[float] = None
    light_calls: int = 0
    advanced_calls: int = 0
    estimated_cost_usd: Optional[float] = None
    cost_available_for: int = 0
    average_latency_ms: Optional[int] = None
    success_rate_percent: Optional[float] = None
    month_spend_usd: float = 0.0
    monthly_budget_usd: Optional[float] = None


class ControlTowerMission(BaseModel):
    mission_id: str
    status: str
    role: str
    organization: Optional[str] = None
    goal: str
    created_at: datetime
    intelligence: str
    models: List[str]
    ai_calls: int
    estimated_cost_usd: Optional[float] = None
    latency_ms: Optional[int] = None
    approvals_required: int = 0


class AIBudgetBody(BaseModel):
    monthly_budget_usd: Optional[float] = Field(default=None, ge=0)


class CatalogAgent(BaseModel):
    key: str
    name: str
    purpose: str
    status: str
    intelligence_level: str
    allowed_roles: List[str]
    requires_approval: bool
    deployable: bool = True


class AgentCatalogView(BaseModel):
    agents: List[CatalogAgent]
    internal_components: List[CatalogAgent]
    flow: List[str]


class AIOperations(BaseModel):
    provider: str
    model: Optional[str] = None
    live: bool
    live_ai_configured: Dict[str, bool]
    missions_by_status: Dict[str, int]
    recent_missions: List[MissionSummary]
    agent_runs_total: int
    agent_runs_failed: int
    failed_missions: int
    provider_errors: int
    rate_limit_incidents: int
    recent_provider_errors: List[OpsEvent]
    pending_approvals: int
    stale_approvals: int
    workflow_failures: int
    recent_workflow_failures: List[OpsEvent]
    routing: Dict[str, str] = {}
    usage: AIUsageSummary = AIUsageSummary()
    control_tower: List[ControlTowerMission] = []
    requests_needing_review: int
    rag_chunks: int
    rag_ready: bool
    database_ready: bool
    token_usage: str = "Recorded per AI call when the provider reports it (Groq does); otherwise shown as unavailable, never estimated."


class AuditEntry(BaseModel):
    timestamp: datetime
    source: str  # mission / operations
    actor: str
    role: str
    action: str
    target: str
    reference: str
    outcome: str


class SystemComponent(BaseModel):
    name: str
    status: Literal["ready", "degraded", "unavailable"]
    detail: str


class SystemStatus(BaseModel):
    overall: Literal["ready", "degraded"]
    components: List[SystemComponent]
    settings: Dict[str, Any]


# ---------------------------------------------------------------------------
# Admin agent planning (LLM structured output)
# ---------------------------------------------------------------------------

AdminIntent = Literal[
    "classes_not_started", "classes_running", "classes_today", "attendance_risk_by_department", "below_threshold",
    "pending_requests", "escalated_requests", "complaints_breached", "failed_workflows", "upcoming_events",
    "campus_overview", "make_request", "unknown",
]


class AdminQueryPlan(BaseModel):
    intent: AdminIntent = Field(
        description=(
            "classes_not_started: classes that have not started / did not start on time today. "
            "classes_running: classes in session right now. classes_today: classes scheduled today. "
            "attendance_risk_by_department: which departments have the most students below the attendance requirement. "
            "below_threshold: students below the attendance requirement. "
            "pending_requests: workflow requests still pending anywhere. "
            "escalated_requests: requests escalated to the HOD or the administration. "
            "complaints_breached: complaints that breached their SLA. "
            "failed_workflows: failed missions, agent errors or provider outages. "
            "upcoming_events: campus events coming up. "
            "campus_overview: whether the campus is running normally today (a broad check). "
            "make_request: the administrator wants to ASK for something. unknown: anything else."
        )
    )
    department_reference: Optional[str] = Field(
        default=None, max_length=20, description="A department code named in the question (e.g. 'CSE'), if any. Never invent one.",
    )
