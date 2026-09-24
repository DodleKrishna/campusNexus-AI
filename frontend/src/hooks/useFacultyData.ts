import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/api/endpoints";
import type { FacultyClassDetail, MarkStatus } from "@/types/api";

const STALE = 30_000;

export const useFacultyProfile = () => useQuery({ queryKey: queryKeys.facultyProfile, queryFn: api.facultyProfile, staleTime: 5 * 60_000 });
// Class status moves with the clock (start windows open and close): refresh every minute.
export const useFacultyDashboard = () =>
  useQuery({ queryKey: queryKeys.facultyDashboard, queryFn: api.facultyDashboard, staleTime: STALE, refetchInterval: 60_000 });
export const useFacultyToday = () =>
  useQuery({ queryKey: queryKeys.facultyToday, queryFn: api.facultyToday, staleTime: STALE, refetchInterval: 60_000 });
export const useFacultyAttendance = () => useQuery({ queryKey: queryKeys.facultyAttendance, queryFn: api.facultyAttendance, staleTime: STALE });
export const useFacultyClass = (sessionId: number) =>
  useQuery({ queryKey: queryKeys.facultyClass(sessionId), queryFn: () => api.facultyClass(sessionId), enabled: Number.isFinite(sessionId), staleTime: 10_000 });

export type ClassAction =
  | { kind: "start" }
  | { kind: "close" }
  | { kind: "cancel" }
  | { kind: "mark"; marks: { student_id: string; status: MarkStatus }[] }
  | { kind: "markAll"; status: MarkStatus };

/** Every class write is an explicit, authenticated API call; the server applies the rules. */
export function useClassAction(sessionId: number) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (action: ClassAction): Promise<FacultyClassDetail> => {
      switch (action.kind) {
        case "start":
          return api.startClass(sessionId);
        case "close":
          return api.closeClass(sessionId);
        case "cancel":
          return api.cancelClass(sessionId);
        case "mark":
          return api.markAttendance(sessionId, action.marks);
        case "markAll":
          return api.markAll(sessionId, action.status);
      }
    },
    onSuccess: (detail) => {
      client.setQueryData(queryKeys.facultyClass(sessionId), detail);
      void client.invalidateQueries({ queryKey: ["faculty"], refetchType: "active" });
    },
  });
}
