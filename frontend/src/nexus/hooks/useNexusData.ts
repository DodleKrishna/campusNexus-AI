import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { api, queryKeys } from "@/api/endpoints";
import { useAdminNotifications } from "@/hooks/useAdminData";
import { useStaffNotifications } from "@/hooks/useHodData";
import { useNotifications, useWorkflowRequests } from "@/hooks/useStudentData";
import { missionPhase } from "@/nexus/lib/activity";
import type { Role } from "@/types/api";

/** The signed-in user's Nexus missions (newest first), refreshed while the page is open. */
export const useAssistantMissions = () =>
  useQuery({ queryKey: queryKeys.assistantMissions, queryFn: () => api.assistantMissions(30), staleTime: 15_000, refetchInterval: 30_000 });

export const useMissionSteps = (missionId: number | null) =>
  useQuery({
    queryKey: queryKeys.missionSteps(missionId ?? 0),
    queryFn: () => api.missionSteps(missionId!),
    enabled: missionId !== null,
    staleTime: 30_000,
  });

export function useActiveMissions() {
  const query = useAssistantMissions();
  const active = useMemo(() => (query.data ?? []).filter((m) => ["active", "waiting"].includes(missionPhase(m.status))), [query.data]);
  return { ...query, active };
}

/** The role's own notification feed (student, staff or admin). */
export function useAlerts(role: Role) {
  const student = useNotifications(role === "student");
  const staff = useStaffNotifications(role === "faculty" || role === "hod");
  const admin = useAdminNotifications(role === "admin");
  return role === "admin" ? admin : role === "student" ? student : staff;
}

export interface FollowUp {
  key: string;
  title: string;
  detail: string;
  /** ISO time the follow-up is about (exam start, request submission). */
  at: string;
  kind: "exam" | "request" | "review";
}

/**
 * What is coming up next for this person: a student's upcoming exams and open
 * requests; a reviewer's pending requests to decide. Read-only, from the API.
 */
export function useFollowUps(role: Role) {
  const isStudent = role === "student";
  const exams = useQuery({ queryKey: queryKeys.exams, queryFn: api.exams, staleTime: 60_000, enabled: isStudent });
  const requests = useWorkflowRequests("inbox");
  const examData = isStudent ? exams.data : undefined;
  // "Upcoming" is judged at fetch time (the query refreshes), keeping render pure.
  const fetchedAt = exams.dataUpdatedAt;
  const items = useMemo<FollowUp[]>(() => {
    const now = fetchedAt;
    const upcoming = (examData ?? [])
      .filter((e) => new Date(e.starts_at).getTime() > now)
      .slice(0, 3)
      .map<FollowUp>((e) => ({ key: `exam-${e.course_code}-${e.starts_at}`, title: `${e.course_title} ${e.exam_type}`.trim(), detail: e.venue, at: e.starts_at, kind: "exam" }));
    const open = (requests.data ?? [])
      .filter((r) => r.status === "pending" || r.status === "needs_review")
      .slice(0, 4)
      .map<FollowUp>((r) => ({
        key: `req-${r.request_id}`,
        title: r.title,
        detail: isStudent ? `${r.type_label} · awaiting ${r.reviewer_name ?? "routing"}` : `${r.type_label} · from ${r.requester_name ?? r.student_name ?? "a requester"}`,
        at: r.submitted_at ?? r.created_at,
        kind: isStudent ? "request" : "review",
      }));
    return [...upcoming, ...open];
  }, [examData, fetchedAt, requests.data, isStudent]);
  return { items, isLoading: requests.isLoading || (isStudent && exams.isLoading), isError: requests.isError };
}
