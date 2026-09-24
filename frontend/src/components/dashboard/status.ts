import type { BadgeTone } from "@/components/ui/badge";
import type { AttendanceStanding, SlotStatus } from "@/types/api";

/** Labels and tones for statuses the backend has already decided. */
export const STANDING: Record<AttendanceStanding, { label: string; tone: BadgeTone }> = {
  good: { label: "Good", tone: "success" },
  at_risk: { label: "At risk", tone: "warning" },
  below_requirement: { label: "Below requirement", tone: "danger" },
  unknown: { label: "Not available", tone: "neutral" },
};

export const SLOT: Record<SlotStatus, { label: string; tone: BadgeTone }> = {
  now: { label: "Now", tone: "accent" },
  upcoming: { label: "Upcoming", tone: "info" },
  completed: { label: "Completed", tone: "neutral" },
};

export const ELIGIBILITY: Record<string, { label: string; tone: BadgeTone }> = {
  eligible: { label: "Eligible", tone: "success" },
  not_eligible: { label: "Not eligible", tone: "danger" },
  unknown: { label: "Unknown", tone: "neutral" },
};

export const REQUEST_STATUS: Record<string, { label: string; tone: BadgeTone }> = {
  pending: { label: "Awaiting approval", tone: "warning" },
  approved: { label: "Approved", tone: "success" },
  rejected: { label: "Rejected", tone: "danger" },
  stale: { label: "Expired", tone: "neutral" },
  edit_required: { label: "Replaced", tone: "neutral" },
};

// Phase 16
export const SESSION: Record<string, { label: string; tone: BadgeTone }> = {
  scheduled: { label: "Scheduled", tone: "info" },
  active: { label: "Live", tone: "accent" },
  closed: { label: "Closed", tone: "neutral" },
  cancelled: { label: "Cancelled", tone: "danger" },
};

export const MARK: Record<string, { label: string; tone: BadgeTone }> = {
  present: { label: "Present", tone: "success" },
  late: { label: "Late", tone: "warning" },
  absent: { label: "Absent", tone: "danger" },
  excused: { label: "Excused", tone: "info" },
  not_marked: { label: "Not marked yet", tone: "neutral" },
};

export const WORKFLOW_STATUS: Record<string, { label: string; tone: BadgeTone }> = {
  draft: { label: "Draft", tone: "neutral" },
  pending: { label: "Pending", tone: "warning" },
  needs_review: { label: "Needs review", tone: "warning" },
  approved: { label: "Approved", tone: "success" },
  rejected: { label: "Rejected", tone: "danger" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

// Phase 17: department class state (decided by the backend's class_state rule)
export const CLASS_STATE: Record<string, { label: string; tone: BadgeTone }> = {
  upcoming: { label: "Upcoming", tone: "info" },
  due: { label: "Due now", tone: "info" },
  delayed: { label: "Not started", tone: "danger" },
  not_held: { label: "Not held", tone: "danger" },
  active: { label: "Active", tone: "accent" },
  completed: { label: "Completed", tone: "neutral" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};
