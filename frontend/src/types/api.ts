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
  agent_key: Exclude<AgentKey, "permission">;
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
