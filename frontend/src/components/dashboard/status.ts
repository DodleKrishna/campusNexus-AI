import type { BadgeTone } from "@/components/ui/badge";
import type { AttendanceStanding, SlotStatus } from "@/types/api";

/**
 * Labels and tones for statuses the backend has already decided. The UI never
 * derives a status; it only chooses how to show it. Tones follow one vocabulary:
 *   live     live · active · now (blue, pulsing)
 *   success  completed · approved · verified · present · eligible · ready
 *   warning  pending · at risk · late (amber)
 *   caution  delayed · due · needs review · degraded (orange)
 *   danger   rejected · failed · conflict · SLA breached · absent · below requirement · not held
 *   neutral  draft · closed · cancelled · scheduled · upcoming · not marked
 */
export type StatusInfo = { label: string; tone: BadgeTone; pulse?: boolean };

export const STANDING: Record<AttendanceStanding, StatusInfo> = {
  good: { label: "Good", tone: "success" },
  at_risk: { label: "At risk", tone: "warning" },
  below_requirement: { label: "Below requirement", tone: "danger" },
  unknown: { label: "Not available", tone: "neutral" },
};

export const SLOT: Record<SlotStatus, StatusInfo> = {
  now: { label: "Live", tone: "live", pulse: true },
  upcoming: { label: "Upcoming", tone: "neutral" },
  completed: { label: "Completed", tone: "success" },
};

export const ELIGIBILITY: Record<string, StatusInfo> = {
  eligible: { label: "Eligible", tone: "success" },
  not_eligible: { label: "Not eligible", tone: "danger" },
  unknown: { label: "Unknown", tone: "neutral" },
};

export const REQUEST_STATUS: Record<string, StatusInfo> = {
  pending: { label: "Awaiting approval", tone: "warning" },
  approved: { label: "Approved", tone: "success" },
  rejected: { label: "Rejected", tone: "danger" },
  stale: { label: "Expired", tone: "neutral" },
  edit_required: { label: "Replaced", tone: "neutral" },
};

// Phase 16
export const SESSION: Record<string, StatusInfo> = {
  scheduled: { label: "Scheduled", tone: "neutral" },
  active: { label: "Live", tone: "live", pulse: true },
  closed: { label: "Closed", tone: "neutral" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

export const MARK: Record<string, StatusInfo> = {
  present: { label: "Present", tone: "success" },
  late: { label: "Late", tone: "warning" },
  absent: { label: "Absent", tone: "danger" },
  excused: { label: "Excused", tone: "neutral" },
  not_marked: { label: "Not marked yet", tone: "neutral" },
};

export const WORKFLOW_STATUS: Record<string, StatusInfo> = {
  draft: { label: "Draft", tone: "neutral" },
  pending: { label: "Pending", tone: "warning" },
  needs_review: { label: "Needs review", tone: "caution" },
  approved: { label: "Approved", tone: "success" },
  rejected: { label: "Rejected", tone: "danger" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

// Phase 17: department class state (decided by the backend's class_state rule)
export const CLASS_STATE: Record<string, StatusInfo> = {
  upcoming: { label: "Upcoming", tone: "neutral" },
  due: { label: "Due now", tone: "caution" },
  delayed: { label: "Not started", tone: "caution" },
  not_held: { label: "Not held", tone: "danger" },
  active: { label: "Live", tone: "live", pulse: true },
  completed: { label: "Completed", tone: "success" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

export const COMPONENT_HEALTH: Record<string, StatusInfo> = {
  ready: { label: "Ready", tone: "success" },
  degraded: { label: "Degraded", tone: "caution" },
  unavailable: { label: "Unavailable", tone: "danger" },
};

export const MISSION_STATUS = (status: string): StatusInfo => ({
  label: status.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()),
  tone: status === "failed" ? "danger" : status === "completed" ? "success" : status.includes("await") || status.includes("pending") ? "warning" : "neutral",
});

export const statusOf = (map: Record<string, StatusInfo>, key: string): StatusInfo => map[key] ?? { label: key.replace(/_/g, " "), tone: "neutral" };
