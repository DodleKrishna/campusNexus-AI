/**
 * Turns persisted AgentOS steps into the safe activity trail shown under a
 * Nexus reply ("Academic Agent consulted", "Response generated", ...).
 *
 * Only identities and enumerated statuses are read: the tool/agent name, the
 * specialist key, a verification status and a source count. A step's free-form
 * inputs and outputs (the user's question, the brain's message, tool data) are
 * never shown, and there is no reasoning to show — the kernel stores none.
 */
import type { AgentMissionStatus, AgentStepView, AutonomousMission } from "@/types/api";

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

// --- Autonomous Guardian / Communication missions --------------------------------------------------------------

/** Request → work → result (a Nexus request answered by specialist agents). */
export const SPECIALIST_LIFECYCLE = ["Request", "Work", "Result"] as const;
/** Monitor → wait → wake → act → verify (an autonomous Guardian or the Communication Agent). */
export const GUARDIAN_LIFECYCLE = ["Monitor", "Wait", "Wake", "Act", "Verify"] as const;

export function specialistStage(status: AgentMissionStatus): number {
  return missionPhase(status) === "done" || missionPhase(status) === "failed" ? 2 : 1;
}

export function guardianStage(mission: Pick<AutonomousMission, "status" | "step_count">): number {
  const phase = missionPhase(mission.status);
  if (phase === "done" || phase === "failed") return 4;
  if (phase === "waiting") return 1;
  if (mission.status === "running") return 3;
  return mission.step_count === 0 ? 0 : 2; // pending: created (monitoring starts) or due to wake
}

const GUARDIAN_TOOLS: Record<string, { label: string; icon: TrailIcon }> = {
  get_assignment_state: { label: "Checked the assignment", icon: "guardian" },
  get_assignment_progress: { label: "Checked submissions", icon: "guardian" },
  get_pending_students: { label: "Listed pending submissions", icon: "guardian" },
  get_recent_assignment_events: { label: "Read new events", icon: "data" },
  get_student_followup_state: { label: "Checked follow-up policy", icon: "data" },
  request_student_followup: { label: "Requested a follow-up", icon: "agent" },
  get_exam_state: { label: "Checked the exam", icon: "guardian" },
  get_exam_progress: { label: "Checked exam attendance", icon: "guardian" },
  get_exam_absentees: { label: "Listed unresolved students", icon: "guardian" },
  get_exam_followup_state: { label: "Checked follow-up policy", icon: "data" },
  get_recent_exam_events: { label: "Read new events", icon: "data" },
  request_exam_followup: { label: "Requested a follow-up", icon: "agent" },
  get_attendance_case: { label: "Checked the absence", icon: "guardian" },
  get_student_attendance_summary: { label: "Checked attendance standing", icon: "guardian" },
  get_recent_attendance_events: { label: "Read new events", icon: "data" },
  get_attendance_followup_state: { label: "Checked follow-up policy", icon: "data" },
  request_attendance_followup: { label: "Requested a follow-up", icon: "agent" },
  get_communication_job: { label: "Checked the delivery job", icon: "data" },
  get_allowed_channels: { label: "Checked permitted channels", icon: "data" },
  request_delivery: { label: "Requested delivery", icon: "agent" },
};

/** Trail items from a Guardian's step identities (kind, tool, status) — the API sends nothing else. */
export function autonomousTrail(mission: AutonomousMission): TrailItem[] {
  const actor = mission.agent_label;
  return mission.activity.map((step) => {
    const key = `auto-${mission.mission_id}-${step.step_number}`;
    if (step.status === "rejected") return { key, actor: "Safety checks", label: "Blocked a disallowed action", tone: "rose", icon: "blocked" };
    const failed = step.status === "failed";
    switch (step.action_type) {
      case "tool": {
        const known = GUARDIAN_TOOLS[step.tool_name ?? ""] ?? { label: `Used ${humanize(step.tool_name || "a tool")}`, icon: "data" as const };
        return { key, actor, ...known, label: failed ? `${known.label} — unavailable` : known.label, tone: failed ? "amber" : "cyan" };
      }
      case "wait":
        return { key, actor, label: "Waiting for the next checkpoint", tone: "amber", icon: "wait" };
      case "complete":
        return { key, actor, label: "Verified and completed", tone: "signal", icon: "done" };
      case "fail":
        return { key, actor, label: "Stopped", tone: "rose", icon: "blocked" };
      default:
        return { key, actor, label: humanize(step.action_type), tone: "cyan", icon: "guardian" };
    }
  });
}

const OWN_STATUS_LABELS: Record<string, string> = {
  submitted: "Submitted on time",
  late: "Submitted late",
  pending: "Pending",
  cancelled: "Cancelled",
  present: "Present",
  absent: "Absent",
  exempt: "Exempt",
  makeup_completed: "Makeup completed",
  open: "Open",
  resolved: "Resolved",
  unresolved: "Unresolved",
  ready: "Ready to deliver",
  in_progress: "Delivering",
  delivered: "Delivered",
  acknowledged: "Acknowledged",
  failed: "Not delivered",
  deferred: "Deferred",
};

const PENDING_NOUN: Record<AutonomousMission["subject"]["type"], string> = {
  assignment: "submission",
  exam: "student",
  attendance: "resolution",
  communication: "delivery",
};

/** One structured progress line ("Waiting for 2 submissions", "Your status: Submitted on time"). */
export function progressLabel(mission: AutonomousMission): string | null {
  const p = mission.progress;
  if (!p) return null;
  if (p.scope === "self" || mission.subject.type === "communication") {
    const status = p.own_status ? (OWN_STATUS_LABELS[p.own_status] ?? humanize(p.own_status)) : null;
    if (!status) return null;
    return mission.subject.type === "communication" ? `Follow-up ${status.toLowerCase()}` : `Your status: ${status}`;
  }
  if (p.total == null) return null;
  const noun = PENDING_NOUN[mission.subject.type];
  if (p.pending) return `Waiting for ${p.pending} ${noun}${p.pending === 1 ? "" : "s"} · ${p.resolved ?? 0} of ${p.total} resolved`;
  return `All ${p.total} resolved`;
}

export function resultLabel(code: string | null): string | null {
  return code ? humanize(code.toLowerCase()) : null;
}

export interface AutonomousStatus {
  /** "Waiting", "Woke at checkpoint", "Reminder delivered", ... */
  headline: string;
  /** A time detail ("Next check 14:30", "at 14:03"), or null. */
  detail: string | null;
  tone: "amber" | "cyan" | "signal" | "rose" | "dim";
}

/** How long a wake-up is shown as "Woke at checkpoint" before the mission's own state takes over again. */
export const RECENT_WAKE_MS = 3 * 60_000;

const COMMUNICATION_HEADLINES: Record<string, { headline: string; tone: AutonomousStatus["tone"] }> = {
  pending: { headline: "Choosing a channel", tone: "cyan" },
  ready: { headline: "Ready to deliver", tone: "cyan" },
  in_progress: { headline: "Delivering", tone: "cyan" },
  deferred: { headline: "Delivery deferred", tone: "amber" },
  delivered: { headline: "delivered", tone: "signal" },
  acknowledged: { headline: "acknowledged", tone: "signal" },
  failed: { headline: "Not delivered", tone: "rose" },
  cancelled: { headline: "Cancelled — no longer needed", tone: "dim" },
};

/**
 * One status line for an autonomous mission, from structured fields only (status, job state, wake time).
 * Never invents an outcome: "delivered" appears only when the API reports the job delivered.
 */
export function autonomousStatus(mission: AutonomousMission, now: number = Date.now()): AutonomousStatus {
  const at = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : null);
  if (mission.kind === "communication") {
    const own = mission.progress?.own_status ?? "";
    const known = COMMUNICATION_HEADLINES[own];
    const noun = /reminder/i.test(mission.subject.title) ? "Reminder" : /absence|absent/i.test(mission.subject.title) ? "Absence notice" : "Message";
    if (known && (own === "delivered" || own === "acknowledged")) return { headline: `${noun} ${known.headline}`, detail: at(mission.completed_at ?? mission.updated_at), tone: known.tone };
    if (known) return { headline: known.headline, detail: null, tone: known.tone };
    return { headline: MISSION_STATUS_LABELS[mission.status] ?? humanize(mission.status), detail: null, tone: "cyan" };
  }
  const woke = mission.last_wake_at ? new Date(mission.last_wake_at).getTime() : null;
  const phase = missionPhase(mission.status);
  if (woke !== null && now - woke < RECENT_WAKE_MS && phase !== "done" && phase !== "failed") {
    const why = mission.last_wake_trigger === "checkpoint" ? "Woke at checkpoint" : "Woke on a new event";
    return { headline: why, detail: at(mission.last_wake_at), tone: "cyan" };
  }
  if (phase === "waiting") return { headline: "Waiting", detail: mission.next_wake_at ? `Next check ${at(mission.next_wake_at)}` : null, tone: "amber" };
  if (phase === "active") return { headline: "Working", detail: null, tone: "cyan" };
  if (phase === "failed") return { headline: MISSION_STATUS_LABELS[mission.status] ?? "Stopped", detail: null, tone: "rose" };
  return { headline: MISSION_STATUS_LABELS[mission.status] ?? "Completed", detail: at(mission.completed_at), tone: "signal" };
}
