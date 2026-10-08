/**
 * Turns persisted AgentOS steps into the safe activity trail shown under a
 * Nexus reply ("Academic Agent consulted", "Response generated", ...).
 *
 * Only identities and enumerated statuses are read: the tool/agent name, the
 * specialist key, a verification status and a source count. A step's free-form
 * inputs and outputs (the user's question, the brain's message, tool data) are
 * never shown, and there is no reasoning to show — the kernel stores none.
 */
import type { AgentMissionStatus, AgentStepView } from "@/types/api";

export type TrailTone = "cyan" | "violet" | "signal" | "amber" | "rose";
export type TrailIcon = "nexus" | "agent" | "data" | "guardian" | "wait" | "human" | "done" | "blocked";

export interface TrailItem {
  key: string;
  /** Who acted ("Nexus", "Academic Agent", ...). */
  actor: string;
  /** What happened, in a few words. */
  label: string;
  tone: TrailTone;
  icon: TrailIcon;
  /** A short structured badge (e.g. "Verified · 2 sources"). */
  detail?: string;
  latencyMs?: number | null;
}

/** Specialist keys accepted by Nexus' consult tool → the names students see. */
export const SPECIALIST_NAMES: Record<string, string> = {
  academic: "Academic Agent",
  placements: "Career Agent",
  events: "Events Agent",
  complaints: "Campus Services Agent",
};

const TOOL_LABELS: Record<string, { actor: string; label: string; icon: TrailIcon }> = {
  get_my_identity_context: { actor: "Nexus", label: "Checked your profile", icon: "data" },
  get_my_active_missions: { actor: "Nexus", label: "Reviewed your active missions", icon: "data" },
  get_my_assignments: { actor: "Assignments", label: "Checked assignment status", icon: "guardian" },
  get_assignment_status: { actor: "Assignments", label: "Checked submission status", icon: "guardian" },
  get_my_exams: { actor: "Exams", label: "Checked the exam schedule", icon: "guardian" },
  get_my_attendance_summary: { actor: "Attendance", label: "Checked attendance standing", icon: "guardian" },
};

const VERIFICATION_LABELS: Record<string, string> = {
  verified: "Verified",
  needs_review: "Needs review",
  failed: "Not verified",
  not_applicable: "Informational",
};

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function humanize(identifier: string): string {
  return identifier.replace(/_agent$/, "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function consultDetail(step: AgentStepView): string | undefined {
  const data = asRecord(asRecord(step.output_summary).data);
  const status = typeof data.verification_status === "string" ? VERIFICATION_LABELS[data.verification_status] : undefined;
  const sources = Array.isArray(data.sources) ? data.sources.length : 0;
  const parts = [status, sources ? `${sources} source${sources === 1 ? "" : "s"}` : undefined].filter(Boolean);
  return parts.length ? parts.join(" · ") : undefined;
}

export function stepToTrail(step: AgentStepView): TrailItem {
  const key = `step-${step.id}`;
  if (step.status === "rejected") {
    return { key, actor: "Safety checks", label: "Blocked a disallowed action", tone: "rose", icon: "blocked" };
  }
  const failed = step.status === "failed";
  switch (step.action_type) {
    case "tool": {
      const name = step.tool_name ?? "";
      if (name === "consult_domain_specialist") {
        const specialist = asRecord(asRecord(step.input_summary).tool_input).specialist;
        const actor = typeof specialist === "string" ? (SPECIALIST_NAMES[specialist] ?? humanize(specialist)) : "Specialist agent";
        return {
          key, actor, label: failed ? "Could not answer" : "Consulted with evidence", tone: failed ? "amber" : "violet",
          icon: "agent", detail: failed ? undefined : consultDetail(step), latencyMs: step.latency_ms,
        };
      }
      const known = TOOL_LABELS[name] ?? { actor: "Nexus", label: `Used ${humanize(name || "a tool")}`, icon: "data" as const };
      return { key, ...known, label: failed ? `${known.label} — unavailable` : known.label, tone: failed ? "amber" : "cyan", latencyMs: step.latency_ms };
    }
    case "delegate":
      return { key, actor: humanize(step.delegated_agent ?? "agent"), label: "Took over a sub-task", tone: "violet", icon: "agent" };
    case "wait":
      return { key, actor: "Nexus", label: "Scheduled a follow-up check", tone: "amber", icon: "wait" };
    case "ask_human":
      return { key, actor: "Nexus", label: "Needs a detail from you", tone: "amber", icon: "human" };
    case "replan":
      return { key, actor: "Nexus", label: "Adjusted the plan", tone: "cyan", icon: "nexus" };
    case "complete":
      return { key, actor: "Nexus", label: "Response generated", tone: "signal", icon: "done" };
    case "fail":
      return { key, actor: "Nexus", label: "Stopped safely", tone: "rose", icon: "blocked" };
    default:
      return { key, actor: "Nexus", label: humanize(step.action_type), tone: "cyan", icon: "nexus" };
  }
}

/** The full trail for one mission: the request intake, then each persisted step in order. */
export function missionTrail(steps: AgentStepView[]): TrailItem[] {
  const ordered = [...steps].sort((a, b) => a.step_number - b.step_number);
  return [
    { key: "intake", actor: "Nexus", label: "Analyzed your request", tone: "cyan", icon: "nexus" },
    ...ordered.map(stepToTrail),
  ];
}

export type MissionPhase = "active" | "waiting" | "done" | "failed";

export function missionPhase(status: AgentMissionStatus): MissionPhase {
  if (status === "completed") return "done";
  if (status === "failed" || status === "cancelled") return "failed";
  if (status.startsWith("waiting")) return "waiting";
  return "active";
}

export const MISSION_STATUS_LABELS: Record<AgentMissionStatus, string> = {
  pending: "Queued",
  running: "Running",
  waiting_event: "Waiting on an event",
  waiting_human: "Waiting for you",
  waiting_connectivity: "Waiting for connectivity",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};
