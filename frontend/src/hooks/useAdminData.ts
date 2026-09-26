import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/api/endpoints";

const STALE = 30_000;
const LIVE = { staleTime: STALE, refetchInterval: 60_000 };

export const useAdminDashboard = (department?: string) =>
  useQuery({ queryKey: queryKeys.admin("dashboard", department), queryFn: () => api.adminDashboard(department), ...LIVE });
export const useAdminDepartments = () => useQuery({ queryKey: queryKeys.admin("departments"), queryFn: api.adminDepartments, ...LIVE });
export const useAdminDepartment = (code: string) =>
  useQuery({ queryKey: queryKeys.admin("department", code), queryFn: () => api.adminDepartment(code), ...LIVE });
export const useAdminAttendance = (department?: string) =>
  useQuery({ queryKey: queryKeys.admin("attendance", department), queryFn: () => api.adminAttendance(department), ...LIVE });
export const useAdminComplaints = (filters: { department?: string; status?: string; priority?: string; breached?: boolean }) =>
  useQuery({
    queryKey: queryKeys.admin("complaints", filters.department, filters.status, filters.priority, filters.breached),
    queryFn: () => api.adminComplaints(filters),
    staleTime: STALE,
  });
export const useAdminUsers = () => useQuery({ queryKey: queryKeys.admin("users"), queryFn: api.adminUsers, staleTime: STALE });
export const useAdminAIOperations = () => useQuery({ queryKey: queryKeys.admin("ai"), queryFn: api.adminAIOperations, ...LIVE });
export const useAdminAgentsCatalog = () => useQuery({ queryKey: queryKeys.admin("agents-catalog"), queryFn: api.adminAgentsCatalog, staleTime: 5_000 });
export const useAdminDeployments = () => useQuery({ queryKey: queryKeys.admin("deployments"), queryFn: api.adminDeployments, staleTime: 5_000 });
export const useAdminAgentCatalog = () => useQuery({ queryKey: queryKeys.admin("agent-catalog"), queryFn: api.adminAgentCatalog, staleTime: STALE });
export const useAdminAudit = (filters: { source?: string; action?: string }) =>
  useQuery({ queryKey: queryKeys.admin("audit", filters.source, filters.action), queryFn: () => api.adminAudit(filters), staleTime: 10_000 });
export const useAdminSystem = () => useQuery({ queryKey: queryKeys.admin("system"), queryFn: api.adminSystem, ...LIVE });
export const useAdminNotifications = (enabled = true) =>
  useQuery({ queryKey: queryKeys.admin("notifications"), queryFn: api.adminNotifications, staleTime: STALE, refetchInterval: 60_000, enabled });
