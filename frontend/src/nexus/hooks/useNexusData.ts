import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { api, queryKeys } from "@/api/endpoints";
import { useAdminNotifications } from "@/hooks/useAdminData";
import { useStaffNotifications } from "@/hooks/useHodData";
import { useNotifications, useWorkflowRequests } from "@/hooks/useStudentData";
import { missionPhase, progressLabel } from "@/nexus/lib/activity";
import type { AgentMissionStatus, Role } from "@/types/api";

/** The signed-in user's Nexus missions (newest first), refreshed while the page is open. */
export const useAssistantMissions = () =>
  useQuery({ queryKey: queryKeys.assistantMissions, queryFn: () => api.assistantMissions(30), staleTime: 15_000, refetchInterval: 30_000 });

/** Guardian / Communication missions the caller may see (tenant, role and subject scoped by the API). */
// Guardians wake on their own (worker): poll every 8 s so the rail shows wake-ups and deliveries live.
export const useAutonomousMissions = () =>
  useQuery({ queryKey: queryKeys.autonomousMissions, queryFn: () => api.autonomousMissions(30), staleTime: 4_000, refetchInterval: 8_000 });

export const useMissionSteps = (missionId: number | null) =>
  useQuery({
    queryKey: queryKeys.missionSteps(missionId ?? 0),
    queryFn: () => api.missionSteps(missionId!),
    enabled: missionId !== null,
    staleTime: 30_000,
  });

export interface ActiveMissionItem {
  id: number;
  status: AgentMissionStatus;
  /** "Assignment Guardian" for an autonomous mission; null for the user's own Nexus request. */
  agent: string | null;
  title: string;
  /** A structured progress line (never free text from a model). */
  detail: string | null;
  updated_at: string;
}

/** Running/waiting Nexus requests and autonomous Guardian missions, newest first. */
export function useActiveMissions() {
  const requests = useAssistantMissions();
  const autonomous = useAutonomousMissions();
  const active = useMemo<ActiveMissionItem[]>(() => {
    const live = (status: AgentMissionStatus) => ["active", "waiting"].includes(missionPhase(status));
    return [
      ...(requests.data ?? []).filter((m) => live(m.status)).map<ActiveMissionItem>((m) => ({
        id: m.mission_id, status: m.status, agent: null, title: m.goal, detail: null, updated_at: m.updated_at,
      })),
      ...(autonomous.data?.items ?? []).filter((m) => live(m.status)).map<ActiveMissionItem>((m) => ({
        id: m.mission_id, status: m.status, agent: m.agent_label, title: m.subject.title, detail: progressLabel(m), updated_at: m.updated_at,
      })),
    ].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  }, [requests.data, autonomous.data]);
  return { active, isLoading: requests.isLoading && autonomous.isLoading, isError: requests.isError && autonomous.isError };
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
