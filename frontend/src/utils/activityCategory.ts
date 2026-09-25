import type { ActivityCategory } from "@/components/ui/activity-feed";

/** Maps a free-form category string from the API (notification category, audit action) to a feed category. */
export function activityCategory(raw: string, source?: string): ActivityCategory {
  const v = raw.toLowerCase();
  if (source === "mission" || v.includes("mission") || v.includes("agent") || v.includes("provider")) return "AI";
  if (v.includes("attendance") || v.includes("class") || v.includes("session")) return "Attendance";
  if (v.includes("request") || v.includes("approval") || v.includes("leave")) return "Request";
  if (v.includes("complaint") || v.includes("case") || v.includes("service") || v.includes("sla")) return "Complaint";
  if (v.includes("event") || v.includes("registration")) return "Event";
  if (v.includes("exam") || v.includes("academic") || v.includes("course")) return "Academic";
  if (v.includes("login") || v.includes("account") || v.includes("role") || v.includes("password") || v.includes("system")) return "System";
  return "Update";
}
