import { useQuery } from "@tanstack/react-query";
import { api, queryKeys, type RequestBox } from "@/api/endpoints";

const STALE = 60_000;

export const useDashboard = () => useQuery({ queryKey: queryKeys.dashboard, queryFn: api.dashboard, staleTime: STALE });
export const useProfile = () => useQuery({ queryKey: queryKeys.profile, queryFn: api.profile, staleTime: 5 * STALE });
export const useAttendance = () => useQuery({ queryKey: queryKeys.attendance, queryFn: api.attendance, staleTime: STALE });
export const useExams = () => useQuery({ queryKey: queryKeys.exams, queryFn: api.exams, staleTime: STALE });
export const useRequests = () => useQuery({ queryKey: queryKeys.requests, queryFn: api.requests, staleTime: STALE });
// Guardian reminders arrive without any user action: refresh every 10 s so they appear without a reload.
export const useNotifications = (enabled = true) =>
  useQuery({ queryKey: queryKeys.notifications, queryFn: api.notifications, staleTime: 5_000, refetchInterval: 10_000, enabled });
// Slot status (upcoming / now / completed) moves with the clock: refresh every minute.
export const useTodaySchedule = () =>
  useQuery({ queryKey: queryKeys.todaySchedule, queryFn: api.todaySchedule, refetchInterval: 60_000, staleTime: 30_000 });
// Connectivity (online / edge offline) can change at any time: refresh every 10 s.
export const useHealth = () =>
  useQuery({ queryKey: queryKeys.health, queryFn: api.health, staleTime: 5_000, refetchInterval: 10_000, retry: 1 });
// Phase 16: the live class moves when the faculty starts or closes it -- refresh every 30 s.
export const useLiveClass = () =>
  useQuery({ queryKey: queryKeys.liveClass, queryFn: api.liveClass, refetchInterval: 30_000, staleTime: 15_000 });
export const useWorkflowRequests = (box: RequestBox = "inbox") =>
  useQuery({ queryKey: queryKeys.workflowBox(box), queryFn: () => api.workflowRequests(box), staleTime: 15_000, refetchInterval: 60_000 });
