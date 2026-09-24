import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/api/endpoints";

const STALE = 30_000;
// Class state (running / delayed / not held) moves with the clock: refresh every minute.
const LIVE = { staleTime: STALE, refetchInterval: 60_000 };

export const useHodDashboard = () => useQuery({ queryKey: queryKeys.hod("dashboard"), queryFn: api.hodDashboard, ...LIVE });
export const useHodDepartment = () => useQuery({ queryKey: queryKeys.hod("department"), queryFn: api.hodDepartment, ...LIVE });
export const useHodFaculty = () => useQuery({ queryKey: queryKeys.hod("faculty"), queryFn: api.hodFaculty, ...LIVE });
export const useHodStudents = () => useQuery({ queryKey: queryKeys.hod("students"), queryFn: api.hodStudents, staleTime: STALE });
export const useHodAttendance = () => useQuery({ queryKey: queryKeys.hod("attendance"), queryFn: api.hodAttendance, ...LIVE });
export const useHodComplaints = () => useQuery({ queryKey: queryKeys.hod("complaints"), queryFn: api.hodComplaints, staleTime: STALE });
export const useStaffNotifications = (enabled = true) =>
  useQuery({ queryKey: queryKeys.staffNotifications, queryFn: api.staffNotifications, staleTime: STALE, refetchInterval: 60_000, enabled });
