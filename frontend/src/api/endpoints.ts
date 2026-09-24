/** Typed backend endpoints. Identity always comes from the bearer token. */
import { apiRequest } from "@/api/client";
import type {
  AgentCatalog,
  AgentQueryResponse,
  AuthUser,
  CourseAttendance,
  DashboardSummary,
  ExamItem,
  Health,
  LoginResponse,
  NotificationItem,
  RequestItem,
  StudentProfile,
  TodaySchedule,
} from "@/types/api";

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
  exams: () => apiRequest<ExamItem[]>("/me/exams"),
  requests: () => apiRequest<RequestItem[]>("/me/requests"),
  notifications: () => apiRequest<NotificationItem[]>("/me/notifications"),

  agentCatalog: () => apiRequest<AgentCatalog>("/agents"),
  askAgent: (agentKey: string, message: string) =>
    apiRequest<AgentQueryResponse>(`/agents/${encodeURIComponent(agentKey)}/query`, { method: "POST", body: { message } }),
};

export const queryKeys = {
  health: ["health"] as const,
  me: ["auth", "me"] as const,
  profile: ["student", "profile"] as const,
  dashboard: ["student", "dashboard"] as const,
  attendance: ["student", "attendance"] as const,
  todaySchedule: ["student", "timetable", "today"] as const,
  exams: ["student", "exams"] as const,
  requests: ["student", "requests"] as const,
  notifications: ["student", "notifications"] as const,
  agentCatalog: ["agents", "catalog"] as const,
};
