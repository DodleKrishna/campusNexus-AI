/** Response shapes of the CampusNexus API (mirrors the backend Pydantic models). */

export type Role = "student" | "faculty" | "hod" | "admin" | "staff";

export interface AuthUser {
  id: number;
  email: string;
  role: Role;
  display_name: string;
  student_id: string | null;
  department_code: string | null;
  department_name: string | null;
  home_route: string;
  organization?: { id: number; slug: string; name: string };
}

export interface LoginResponse {
  access_token: string;
  token_type: "bearer";
  expires_at: string;
  user: AuthUser;
}

export interface StudentProfile {
  student_code: string;
  full_name: string;
  first_name: string;
  department_code: string;
  department_name: string;
  year: number;
  semester: number;
  cgpa: number;
}

export interface PolicySource {
  document_id: string;
  title: string | null;
  version: string | null;
  section: string | null;
}

export type AttendanceStanding = "good" | "at_risk" | "below_requirement" | "unknown";

export interface CourseAttendance {
  course_code: string;
  course_title: string;
  instructor: string | null;
  classes_attended: number;
  classes_conducted: number;
  current_percentage: number | null;
  required_percentage: number | null;
  eligible_now: boolean | null;
  standing: AttendanceStanding;
  classes_needed_to_reach_threshold: number | null;
  maximum_additional_absences_allowed: number | null;
  threshold_reachable: boolean | null;
  policy: PolicySource | null;
}

export interface ExamItem {
  course_code: string;
  course_title: string;
  exam_type: string;
  starts_at: string;
  ends_at: string;
  venue: string;
  eligibility: "eligible" | "not_eligible" | "unknown" | null;
  eligibility_caveat: string | null;
}

export type SlotStatus = "upcoming" | "now" | "completed";

export interface ScheduleSlot {
  course_code: string;
  course_title: string;
  instructor: string | null;
  start_time: string;
  end_time: string;
  location: string;
  status: SlotStatus;
}

export interface TodaySchedule {
  date: string;
  weekday: string;
  timezone: string;
  now_local: string;
  slots: ScheduleSlot[];
}

export interface RequestItem {
  approval_id: string;
  mission_id: string;
  title: string;
  status: string;
  tool_name: string | null;
  requested_at: string;
  decided_at: string | null;
}

export interface NotificationItem {
  id: number;
  title: string;
  body: string;
  category: string;
  status: string;
  created_at: string;
}

export interface DashboardSummary {
  profile: StudentProfile;
  cgpa: number;
  overall_attendance: { percentage: number; classes_attended: number; classes_conducted: number } | null;
  courses_below_requirement: number;
  next_exam: ExamItem | null;
  pending_requests: number;
  unread_notifications: number;
}

export interface Health {
  ready: boolean;
  llm: { provider: string; live: boolean; model: string | null };
}

// ---------------------------------------------------------------------------
// Agents
// ---------------------------------------------------------------------------

export type AgentKey = "academic" | "events" | "placements" | "complaints" | "enquiry" | "permission";
export type SpecialistKey = "academic" | "events" | "placements" | "complaints";
export type VerificationStatus = "verified" | "needs_review" | "failed" | "not_applicable";

export interface Evidence {
  evidence_id: string;
  document_id: string;
  title: string;
  snippet: string;
  section: string | null;
  policy_version: string | null;
  source: string;
}

/** Structured facts are agent-specific JSON; renderers narrow them. */
export type Facts = Record<string, unknown>;

export interface SpecialistAnswer {
  agent_key: SpecialistKey;
  agent: string;
  objective: string;
  verification_status: VerificationStatus;
  answer: string;
  facts: Facts;
  evidence: Evidence[];
  issues: string[];
}

export interface AgentQueryResponse {
  agent_key: AgentKey;
  display_name: string;
  verification_status: VerificationStatus;
  answer: string;
  facts: Facts;
  evidence: Evidence[];
  issues: string[];
  consulted: SpecialistAnswer[];
  notices: string[];
  action_hint: { agent_key: string; message: string } | null;
  live_ai: boolean;
}

export interface AgentCatalog {
  live_ai: boolean;
  provider: string;
  model: string | null;
  agents: { key: AgentKey; display_name: string; description: string; backend_agent: string | null; available: boolean }[];
}

// ---------------------------------------------------------------------------
// Phase 16: faculty operations, live class status, workflow requests
// ---------------------------------------------------------------------------

export type SessionStatus = "scheduled" | "active" | "closed" | "cancelled";
export type MarkStatus = "present" | "absent" | "late" | "excused";

export interface WeeklySlot {
  weekday: string;
  start_time: string;
  end_time: string;
  room: string;
}

export interface TeachingAssignment {
  assignment_id: number;
  course_code: string;
  course_title: string;
  department_code: string;
  year: number;
  semester: number;
  section: string;
  academic_term: string;
  roster_size: number;
  weekly_slots: WeeklySlot[];
}

export interface FacultyProfile {
  faculty_id: number;
  employee_code: string;
  full_name: string;
  department_code: string;
  department_name: string;
  designation: string;
  email: string;
  phone: string | null;
  assignments: TeachingAssignment[];
}

export interface MarkTally {
  roster: number;
  present: number;
  absent: number;
  late: number;
  excused: number;
  unmarked: number;
}

export interface FacultyClass {
  session_id: number;
  assignment_id: number;
  course_code: string;
  course_title: string;
  department_code: string;
  year: number;
  semester: number;
  section: string;
  session_date: string;
  scheduled_start: string;
  scheduled_end: string;
  start_local: string;
  end_local: string;
  room: string;
  status: SessionStatus;
  is_extra_class: boolean;
  actual_started_at: string | null;
  actual_closed_at: string | null;
  tally: MarkTally;
  can_start: boolean;
  start_blocked_reason: string | null;
}

export interface RosterEntry {
  student_id: string;
  full_name: string;
  mark: MarkStatus | null;
  marked_at: string | null;
  classes_attended: number | null;
  classes_conducted: number | null;
  current_percentage: number | null;
  standing: AttendanceStanding;
}

export interface FacultyClassDetail {
  class_info: FacultyClass;
  roster: RosterEntry[];
  required_percentage: number | null;
}

export interface FacultyDashboard {
  profile: FacultyProfile;
  date: string;
  now_local: string;
  classes_today: number;
  active_class: FacultyClass | null;
  students_across_today: number;
  pending_requests: number;
  today: FacultyClass[];
}

export interface AssignmentStanding {
  assignment: TeachingAssignment;
  required_percentage: number | null;
  below_requirement: number;
  at_risk: number;
  students: RosterEntry[];
}

export interface LiveClassInfo {
  session_id: number | null;
  course_code: string;
  course_title: string;
  section: string;
  faculty_name: string;
  room: string;
  scheduled_start: string;
  scheduled_end: string;
  start_local: string;
  end_local: string;
  session_status: SessionStatus;
  is_extra_class: boolean;
  actual_started_at: string | null;
  actual_closed_at: string | null;
}

export interface LiveClassStatus {
  state: "live" | "scheduled" | "closed" | "cancelled" | "no_class";
  current: LiveClassInfo | null;
  my_attendance: MarkStatus | "not_marked" | null;
  my_attendance_marked_at: string | null;
  next_class: LiveClassInfo | null;
  message: string;
  as_of: string;
  timezone: string;
}

export type WorkflowRequestStatus = "draft" | "pending" | "needs_review" | "approved" | "rejected" | "cancelled";

export interface AffectedClass {
  course_code: string;
  course_title: string;
  section: string;
  starts_at: string;
  ends_at: string;
  start_local: string;
  end_local: string;
  room: string;
  faculty_name: string;
  session_status: SessionStatus;
  my_mark: MarkStatus | null;
  class_label?: string | null;
  roster_size?: number | null;
  substitute?: string | null;
  attendance_percentage: number | null;
  required_percentage: number | null;
  standing: AttendanceStanding;
}

export interface RequestContext {
  event: { event_id: number; title: string; starts_at: string; ends_at: string; location: string } | null;
  request_date: string | null;
  day_part: "full_day" | "morning" | "afternoon" | null;
  window_start: string | null;
  window_end: string | null;
  affected_classes: AffectedClass[];
  timetable_conflict: boolean;
  student_name: string | null;
  student_year: number | null;
  student_section: string | null;
  department_code: string | null;
  requester_kind?: "student" | "faculty";
  faculty_name?: string | null;
  faculty_designation?: string | null;
  faculty_employee_code?: string | null;
  notes: string[];
}

export interface WorkflowRequest {
  request_id: string;
  request_type:
    | "event_permission"
    | "attendance_permission"
    | "leave_request"
    | "od_request"
    | "faculty_leave"
    | "class_substitution"
    | "department_permission"
    | "hod_leave"
    | "department_resource"
    | "admin_escalation";
  type_label: string;
  status: WorkflowRequestStatus;
  title: string;
  reason: string;
  student_id: string | null;
  student_name: string | null;
  requester_kind?: "student" | "faculty";
  requester_name?: string | null;
  requester_role?: string | null;
  department_code?: string | null;
  reviewer_role?: "faculty" | "admin" | null;
  routing_history?: { at: string; basis: string; reviewer: string; note: string }[];
  reviewer_name: string | null;
  routing_basis: "affected_course_faculty" | "mentor" | "unresolved" | "hod_escalation" | "department_hod" | "admin_escalation" | "administration";
  routing_note: string;
  context: RequestContext;
  created_at: string;
  submitted_at: string | null;
  decided_at: string | null;
  decided_by: string | null;
  decision_reason: string | null;
}

export interface PermissionPreview {
  outcome: "draft_ready" | "needs_clarification" | "not_supported";
  message: string;
  request: WorkflowRequest | null;
  options: string[];
  interpretation: Record<string, unknown>;
  live_ai: boolean;
}

export interface FacultyAgentCatalog {
  live_ai: boolean;
  provider: string;
  model: string | null;
  agents: { key: "academic" | "enquiry" | "permission"; display_name: string; description: string; available: boolean }[];
}

// ---------------------------------------------------------------------------
// Phase 17: HOD / department operations
// ---------------------------------------------------------------------------

export type ClassState = "upcoming" | "due" | "delayed" | "not_held" | "active" | "completed" | "cancelled";

export interface HodProfile {
  faculty_id: number;
  employee_code: string;
  full_name: string;
  designation: string;
  email: string;
  department_id: number;
  department_code: string;
  department_name: string;
}

export interface DepartmentClass {
  session_id: number | null;
  assignment_id: number;
  course_code: string;
  course_title: string;
  class_label: string;
  year: number;
  section: string;
  faculty_id: number;
  faculty_name: string;
  room: string;
  scheduled_start: string;
  scheduled_end: string;
  start_local: string;
  end_local: string;
  session_status: SessionStatus;
  state: ClassState;
  is_extra_class: boolean;
  actual_started_at: string | null;
  actual_closed_at: string | null;
  tally: MarkTally | null;
}

export interface DepartmentDashboard {
  profile: HodProfile;
  date: string;
  now_local: string;
  start_grace_minutes: number;
  faculty_count: number;
  student_count: number;
  classes_today: number;
  active_classes: number;
  completed_classes: number;
  not_started_classes: number;
  pending_faculty_requests: number;
  escalated_student_requests: number;
  activity: DepartmentClass[];
}

export interface FacultySummary {
  faculty_id: number;
  employee_code: string;
  full_name: string;
  designation: string;
  email: string;
  is_hod: boolean;
  courses: string[];
  classes_today: number;
  active_class: string | null;
  not_started_today: number;
  pending_requests_to_review: number;
  own_open_requests: number;
}

export interface StudentRisk {
  student_id: string;
  full_name: string;
  year: number;
  semester: number;
  section: string | null;
  overall_percentage: number | null;
  classes_attended: number;
  classes_conducted: number;
  courses_below_threshold: string[];
  open_complaints: number;
}

export interface CourseAttendanceSummary {
  assignment_id: number;
  course_code: string;
  course_title: string;
  class_label: string;
  faculty_name: string;
  students: number;
  classes_attended: number;
  classes_conducted: number;
  percentage: number | null;
  below_threshold: number;
}

export interface SectionAttendanceSummary {
  class_label: string;
  year: number;
  section: string;
  students: number;
  classes_attended: number;
  classes_conducted: number;
  percentage: number | null;
  below_threshold: number;
}

export interface LowAttendanceEntry {
  student_id: string;
  full_name: string;
  class_label: string;
  course_code: string;
  course_title: string;
  classes_attended: number;
  classes_conducted: number;
  percentage: number;
}

export interface SessionSummary {
  session_id: number;
  course_code: string;
  course_title: string;
  class_label: string;
  faculty_name: string;
  session_date: string;
  start_local: string;
  status: SessionStatus;
  actual_started_at: string | null;
  tally: MarkTally;
}

export interface AttendanceInsights {
  required_percentage: number | null;
  sections: SectionAttendanceSummary[];
  courses: CourseAttendanceSummary[];
  low_attendance: LowAttendanceEntry[];
  recent_sessions: SessionSummary[];
  incomplete_sessions: SessionSummary[];
  not_held_today: DepartmentClass[];
}

export interface DepartmentComplaint {
  case_code: string;
  student_id: string;
  student_name: string;
  category: string;
  priority: string;
  status: string;
  office: string;
  created_at: string;
  response_due_at: string | null;
  resolution_due_at: string | null;
  response_breached: boolean;
  resolution_breached: boolean;
}

export interface DepartmentOverview {
  dashboard: DepartmentDashboard;
  faculty: FacultySummary[];
  at_risk_students: number;
  below_threshold_entries: number;
  required_percentage: number | null;
}

// ---------------------------------------------------------------------------
// Phase 18: administrator console
// ---------------------------------------------------------------------------

export interface DepartmentRow {
  department_id: number;
  code: string;
  name: string;
  hod_name: string | null;
  faculty_count: number;
  student_count: number;
  classes_today: number;
  active_classes: number;
  not_started_classes: number;
  attendance_risk_students: number;
  pending_requests: number;
  open_complaints: number;
  sla_breaches: number;
}

export interface AdminDashboard {
  date: string;
  now_local: string;
  start_grace_minutes: number;
  total_students: number;
  total_faculty: number;
  departments: number;
  classes_today: number;
  active_classes: number;
  not_started_classes: number;
  completed_classes: number;
  cancelled_classes: number;
  pending_requests: number;
  escalations: number;
  sla_breaches: number;
  system_errors_24h: number;
  activity: DepartmentClass[];
  department_filter: string | null;
}

export interface DepartmentDetail {
  department: DepartmentRow;
  activity: DepartmentClass[];
  faculty: FacultySummary[];
  at_risk_students: StudentRisk[];
  attendance: AttendanceInsights;
  complaints: DepartmentComplaint[];
}

export interface DepartmentAttendance {
  code: string;
  name: string;
  students: number;
  classes_attended: number;
  classes_conducted: number;
  percentage: number | null;
  students_below_threshold: number;
}

export interface AdminAttendance {
  required_percentage: number | null;
  by_department: DepartmentAttendance[];
  insights: AttendanceInsights;
  active_sessions: SessionSummary[];
  low_attendance_students: number;
}

export interface AdminComplaint extends DepartmentComplaint {
  student_department: string;
}

export interface UserView {
  account_id: number;
  email: string;
  display_name: string;
  role: Role;
  is_active: boolean;
  department_code: string | null;
  linked_student_id: string | null;
  linked_faculty: string | null;
  is_department_head: boolean;
  last_login_at: string | null;
  allowed_roles: Role[];
}

export interface PasswordReset {
  account_id: number;
  temporary_password: string;
  note: string;
}

export interface OpsEvent {
  timestamp: string;
  event_type: string;
  reference: string;
  message: string;
  details: Record<string, unknown>;
}

export interface AIOperations {
  provider: string;
  model: string | null;
  live: boolean;
  live_ai_configured: Record<string, boolean>;
  missions_by_status: Record<string, number>;
  recent_missions: { mission_id: string; user_id: string; goal: string; status: string; created_at: string; updated_at: string }[];
  agent_runs_total: number;
  agent_runs_failed: number;
  failed_missions: number;
  provider_errors: number;
  rate_limit_incidents: number;
  recent_provider_errors: OpsEvent[];
  pending_approvals: number;
  stale_approvals: number;
  workflow_failures: number;
  recent_workflow_failures: OpsEvent[];
  requests_needing_review: number;
  rag_chunks: number;
  rag_ready: boolean;
  database_ready: boolean;
  token_usage: string;
  routing?: Record<string, string>;
  usage?: AIUsageSummary;
  control_tower?: ControlTowerMission[];
  agent_runs?: AgentRunRow[];
}

export interface AIUsageSummary {
  total_requests: number;
  no_ai_count: number;
  no_ai_percent: number | null;
  light_calls: number;
  advanced_calls: number;
  estimated_cost_usd: number | null;
  cost_available_for: number;
  average_latency_ms: number | null;
  success_rate_percent: number | null;
  month_spend_usd: number;
  monthly_budget_usd: number | null;
}

export interface ControlTowerMission {
  mission_id: string;
  status: string;
  role: string;
  organization: string | null;
  goal: string;
  created_at: string;
  intelligence: "no_ai" | "light" | "advanced";
  models: string[];
  ai_calls: number;
  estimated_cost_usd: number | null;
  latency_ms: number | null;
  approvals_required: number;
}

export interface CatalogAgent {
  key: string;
  name: string;
  purpose: string;
  status: string;
  intelligence_level: "no_ai" | "light" | "advanced";
  allowed_roles: string[];
  requires_approval: boolean;
  deployable: boolean;
}

export interface Kpi { key: string; label: string; value: string; note: string }
export interface HealthItem { key: string; label: string; value: number; note: string; link: string }
export interface CommandCenter { kpis: Kpi[]; health: HealthItem[]; workforce: DeploymentView[]; activity: AuditEntry[] }
export interface WorkflowCard {
  key: string; name: string; trigger: string; chain: string[]; sla: string; status: "active" | "partial";
  processed: number; pending: number; escalations: number; note: string | null;
}
export interface MonitorFinding { title: string; detail: string; severity: "info" | "warning" | "critical" }
export interface Monitor {
  key: string; name: string; description: string; findings_count: number; findings: MonitorFinding[]; last_checked: string | null; actions: string;
}
export interface KnowledgeDoc {
  document_id: string; title: string; document_type: string; version: string | null; effective_from: string | null; agents: Record<string, boolean>;
}
export interface KnowledgeHub { documents: KnowledgeDoc[]; indexed_chunks: number; scope_note: string }
export interface Connector {
  key: string; name: string; category: string; description: string; status: "connected" | "available" | "coming_next"; detail: string;
}

export type IntelligenceLevel = "no_ai" | "light" | "advanced";

export interface DeploymentView {
  id: number;
  agent_key: string;
  display_name: string;
  status: "active" | "paused";
  intelligence_level: IntelligenceLevel;
  monthly_budget_usd: number | null;
  requires_approval: boolean;
  allowed_roles: string[];
  runs: number;
  success_rate_percent: number | null;
  estimated_cost_usd: number | null;
  updated_at: string;
}

export interface CatalogEntry extends CatalogAgent {
  deployment: DeploymentView | null;
}

export interface DeploymentConfig {
  status?: "active" | "paused";
  intelligence_level?: IntelligenceLevel;
  monthly_budget_usd?: number | null;
  requires_approval?: boolean;
  allowed_roles?: string[];
}

export interface AgentRunRow {
  run_id: string;
  agent_key: string;
  agent_name: string;
  organization: string | null;
  intelligence: IntelligenceLevel;
  models: string[];
  status: string;
  latency_ms: number | null;
  estimated_cost_usd: number | null;
  requires_approval: boolean;
  created_at: string;
}

export interface AgentCatalogView {
  agents: CatalogAgent[];
  internal_components: CatalogAgent[];
  flow: string[];
}

export interface AuditEntry {
  timestamp: string;
  source: "mission" | "operations";
  actor: string;
  role: string;
  action: string;
  target: string;
  reference: string;
  outcome: string;
}

export interface SystemStatus {
  overall: "ready" | "degraded";
  components: { name: string; status: "ready" | "degraded" | "unavailable"; detail: string }[];
  settings: Record<string, unknown>;
}
