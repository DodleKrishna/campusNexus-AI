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
  notes: string[];
}

export interface WorkflowRequest {
  request_id: string;
  request_type: "event_permission" | "attendance_permission" | "leave_request" | "od_request";
  type_label: string;
  status: WorkflowRequestStatus;
  title: string;
  reason: string;
  student_id: string | null;
  student_name: string | null;
  reviewer_name: string | null;
  routing_basis: "affected_course_faculty" | "mentor" | "unresolved";
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
