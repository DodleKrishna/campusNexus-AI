/** Typed backend endpoints. Identity always comes from the bearer token. */
import { apiRequest } from "@/api/client";
import type {
  AgentStepView,
  AssistantMissionSummary,
  AutonomousMissionPage,
  AssistantReply,
  VoiceAssistantReply,
  AdminAttendance,
  AdminComplaint,
  AdminDashboard,
  AIOperations,
  AIUsageSummary,
  AgentCatalogView,
  CatalogEntry,
  CommandCenter,
  Connector,
  KnowledgeHub,
  Monitor,
  WorkflowCard,
  DeploymentConfig,
  DeploymentView,
  AuditEntry,
  DepartmentDetail,
  DepartmentRow,
  PasswordReset,
  SystemStatus,
  UserView,
  AttendanceInsights,
  DepartmentClass,
  DepartmentComplaint,
  DepartmentDashboard,
  DepartmentOverview,
  FacultySummary,
  HodProfile,
  StudentRisk,
  AgentCatalog,
  AgentQueryResponse,
  AssignmentStanding,
  AuthUser,
  CourseAttendance,
  DashboardSummary,
  ExamItem,
  FacultyAgentCatalog,
  FacultyClass,
  FacultyClassDetail,
  FacultyDashboard,
  FacultyProfile,
  Health,
  LiveClassStatus,
  LoginResponse,
  MarkStatus,
  NotificationItem,
  PermissionPreview,
  RequestItem,
  StudentProfile,
  TodaySchedule,
  WorkflowRequest,
} from "@/types/api";

export type ChatScope = "student" | "faculty" | "hod" | "admin";
export type RequestBox = "inbox" | "mine";
const CHAT_BASE: Record<ChatScope, string> = { student: "/agents", faculty: "/faculty/agents", hod: "/hod/agents", admin: "/admin/agents" };
const qs = (params: Record<string, string | boolean | undefined | null>) => {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "" && v !== false);
  return entries.length ? `?${new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString()}` : "";
};

export const api = {
  health: () => apiRequest<Health>("/health", { auth: false }),

  login: (email: string, password: string) =>
    apiRequest<LoginResponse>("/auth/login", { method: "POST", body: { email, password }, auth: false }),
  me: () => apiRequest<AuthUser>("/auth/me"),
  logout: () => apiRequest<void>("/auth/logout", { method: "POST" }),

  profile: () => apiRequest<StudentProfile>("/me/profile"),
  dashboard: () => apiRequest<DashboardSummary>("/me/dashboard"),
  attendance: () => apiRequest<CourseAttendance[]>("/me/attendance"),
  courseAttendance: (courseCode: string) => apiRequest<CourseAttendance>(`/me/attendance/${encodeURIComponent(courseCode)}`),
  todaySchedule: () => apiRequest<TodaySchedule>("/me/timetable/today"),
  liveClass: () => apiRequest<LiveClassStatus>("/me/live-class"),
  exams: () => apiRequest<ExamItem[]>("/me/exams"),
  requests: () => apiRequest<RequestItem[]>("/me/requests"),
  notifications: () => apiRequest<NotificationItem[]>("/me/notifications"),

  agentCatalog: () => apiRequest<AgentCatalog>("/agents"),
  askAgent: (agentKey: string, message: string, scope: ChatScope = "student") =>
    apiRequest<AgentQueryResponse>(`${CHAT_BASE[scope]}/${encodeURIComponent(agentKey)}/query`, {
      method: "POST",
      body: { message },
    }),

  // Workflow requests (permission / leave / OD) -- students and their reviewers.
  workflowRequests: (box: RequestBox = "inbox") => apiRequest<WorkflowRequest[]>(`/requests?box=${box}`),
  prepareRequest: (message: string) => apiRequest<PermissionPreview>("/requests/prepare", { method: "POST", body: { message } }),
  submitRequest: (requestId: string, reason?: string) =>
    apiRequest<WorkflowRequest>("/requests", { method: "POST", body: { request_id: requestId, ...(reason ? { reason } : {}) } }),
  cancelRequest: (requestId: string) => apiRequest<WorkflowRequest>(`/requests/${encodeURIComponent(requestId)}/cancel`, { method: "POST" }),
  decideRequest: (requestId: string, decision: "approve" | "reject", comment?: string) =>
    apiRequest<WorkflowRequest>(`/requests/${encodeURIComponent(requestId)}/${decision}`, {
      method: "POST",
      body: comment ? { comment } : {},
    }),

  // Faculty workspace.
  facultyProfile: () => apiRequest<FacultyProfile>("/faculty/me"),
  facultyDashboard: () => apiRequest<FacultyDashboard>("/faculty/dashboard"),
  facultyToday: () => apiRequest<FacultyClass[]>("/faculty/classes/today"),
  facultyAttendance: () => apiRequest<AssignmentStanding[]>("/faculty/attendance"),
  facultyClass: (sessionId: number) => apiRequest<FacultyClassDetail>(`/faculty/classes/${sessionId}`),
  startClass: (sessionId: number) => apiRequest<FacultyClassDetail>(`/faculty/classes/${sessionId}/start`, { method: "POST" }),
  closeClass: (sessionId: number) => apiRequest<FacultyClassDetail>(`/faculty/classes/${sessionId}/close`, { method: "POST" }),
  cancelClass: (sessionId: number) => apiRequest<FacultyClassDetail>(`/faculty/classes/${sessionId}/cancel`, { method: "POST", body: {} }),
  markAttendance: (sessionId: number, marks: { student_id: string; status: MarkStatus }[]) =>
    apiRequest<FacultyClassDetail>(`/faculty/classes/${sessionId}/attendance`, { method: "POST", body: { marks } }),
  markAll: (sessionId: number, status: MarkStatus = "present") =>
    apiRequest<FacultyClassDetail>(`/faculty/classes/${sessionId}/attendance/all`, { method: "POST", body: { status } }),
  facultyAgentCatalog: () => apiRequest<FacultyAgentCatalog>("/faculty/agents"),
  staffNotifications: () => apiRequest<NotificationItem[]>("/faculty/notifications"),

  // HOD: department derived from the signed-in account, never passed by the client.
  hodProfile: () => apiRequest<HodProfile>("/hod/me"),
  hodDashboard: () => apiRequest<DepartmentDashboard>("/hod/dashboard"),
  hodDepartment: () => apiRequest<DepartmentOverview>("/hod/department"),
  hodActivity: () => apiRequest<DepartmentClass[]>("/hod/activity"),
  hodFaculty: () => apiRequest<FacultySummary[]>("/hod/faculty"),
  hodStudents: () => apiRequest<StudentRisk[]>("/hod/students"),
  hodAttendance: () => apiRequest<AttendanceInsights>("/hod/attendance"),
  hodComplaints: () => apiRequest<DepartmentComplaint[]>("/hod/complaints"),

  // Administrator console (institution-wide; filters only narrow).
  adminDashboard: (department?: string) => apiRequest<AdminDashboard>(`/admin/dashboard${qs({ department })}`),
  adminDepartments: () => apiRequest<DepartmentRow[]>("/admin/departments"),
  adminDepartment: (code: string) => apiRequest<DepartmentDetail>(`/admin/departments/${encodeURIComponent(code)}`),
  adminAttendance: (department?: string) => apiRequest<AdminAttendance>(`/admin/attendance${qs({ department })}`),
  adminComplaints: (filters: { department?: string; status?: string; priority?: string; breached?: boolean }) =>
    apiRequest<AdminComplaint[]>(`/admin/complaints${qs(filters)}`),
  adminUsers: () => apiRequest<UserView[]>("/admin/users"),
  adminSetActive: (accountId: number, isActive: boolean) =>
    apiRequest<UserView>(`/admin/users/${accountId}/active`, { method: "POST", body: { is_active: isActive } }),
  adminSetRole: (accountId: number, role: string) => apiRequest<UserView>(`/admin/users/${accountId}/role`, { method: "POST", body: { role } }),
  adminResetPassword: (accountId: number) => apiRequest<PasswordReset>(`/admin/users/${accountId}/reset-password`, { method: "POST" }),
  adminAIOperations: () => apiRequest<AIOperations>("/admin/ai-operations"),
  adminSetAIBudget: (monthlyBudgetUsd: number | null) =>
    apiRequest<AIUsageSummary>("/admin/ai-budget", { method: "PUT", body: { monthly_budget_usd: monthlyBudgetUsd } }),
  adminAgentCatalog: () => apiRequest<AgentCatalogView>("/admin/agent-catalog"),
  adminAgentsCatalog: () => apiRequest<CatalogEntry[]>("/admin/agents/catalog"),
  adminCommandCenter: () => apiRequest<CommandCenter>("/admin/command-center"),
  adminWorkflows: () => apiRequest<WorkflowCard[]>("/admin/workflows"),
  adminMonitors: () => apiRequest<Monitor[]>("/admin/monitors"),
  adminRunMonitor: (key: string) => apiRequest<Monitor>(`/admin/monitors/${encodeURIComponent(key)}/run`, { method: "POST" }),
  adminKnowledge: () => apiRequest<KnowledgeHub>("/admin/knowledge"),
  adminConnectors: () => apiRequest<Connector[]>("/admin/connectors"),
  adminDeployments: () => apiRequest<DeploymentView[]>("/admin/agents/deployments"),
  adminDeployAgent: (agentKey: string, config: DeploymentConfig) =>
    apiRequest<DeploymentView>(`/admin/agents/${encodeURIComponent(agentKey)}/deploy`, { method: "POST", body: config }),
  adminConfigureAgent: (deploymentId: number, config: DeploymentConfig) =>
    apiRequest<DeploymentView>(`/admin/agents/deployments/${deploymentId}`, { method: "PATCH", body: config }),
  adminAudit: (filters: { source?: string; action?: string }) => apiRequest<AuditEntry[]>(`/admin/audit${qs(filters)}`),
  adminSystem: () => apiRequest<SystemStatus>("/admin/system"),
  adminNotifications: () => apiRequest<NotificationItem[]>("/admin/notifications"),

  // Nexus personal assistant (AgentOS). Identity, role and organization come from the token only.
  assistantMessage: (message: string, signal?: AbortSignal) =>
    apiRequest<AssistantReply>("/agentos/assistant/message", { method: "POST", body: { message }, signal }),
  assistantVoice: (wav: Blob, replyAudio = true, signal?: AbortSignal) =>
    apiRequest<VoiceAssistantReply>(`/agentos/assistant/voice${qs({ reply_audio: replyAudio ? "true" : "false" })}`, {
      method: "POST", raw: { data: wav, contentType: "audio/wav" }, signal,
    }),
  assistantMissions: (limit = 20) => apiRequest<AssistantMissionSummary[]>(`/agentos/assistant/missions?limit=${limit}`),
  missionSteps: (missionId: number) => apiRequest<AgentStepView[]>(`/agentos/missions/${missionId}/steps`),
  autonomousMissions: (limit = 30) => apiRequest<AutonomousMissionPage>(`/agentos/autonomous-missions?limit=${limit}`),
};

export const queryKeys = {
  health: ["health"] as const,
  me: ["auth", "me"] as const,
  profile: ["student", "profile"] as const,
  dashboard: ["student", "dashboard"] as const,
  attendance: ["student", "attendance"] as const,
  todaySchedule: ["student", "timetable", "today"] as const,
  liveClass: ["student", "live-class"] as const,
  exams: ["student", "exams"] as const,
  requests: ["student", "requests"] as const,
  notifications: ["student", "notifications"] as const,
  agentCatalog: ["agents", "catalog"] as const,
  workflowRequests: ["workflow-requests"] as const,
  workflowBox: (box: RequestBox) => ["workflow-requests", box] as const,
  staffNotifications: ["staff", "notifications"] as const,
  hod: (part: string) => ["hod", part] as const,
  admin: (...parts: (string | number | boolean | undefined)[]) => ["admin", ...parts] as const,
  facultyProfile: ["faculty", "profile"] as const,
  facultyDashboard: ["faculty", "dashboard"] as const,
  facultyToday: ["faculty", "classes", "today"] as const,
  facultyAttendance: ["faculty", "attendance"] as const,
  facultyClass: (sessionId: number) => ["faculty", "class", sessionId] as const,
  assistantMissions: ["agentos", "assistant", "missions"] as const,
  missionSteps: (missionId: number) => ["agentos", "missions", missionId, "steps"] as const,
  autonomousMissions: ["agentos", "autonomous-missions"] as const,
};
