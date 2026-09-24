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
