/** Campus-time formatting. The seeded campus is in India; the API returns UTC. */
export const CAMPUS_TIME_ZONE = "Asia/Kolkata";

const dateFmt = new Intl.DateTimeFormat("en-IN", { timeZone: CAMPUS_TIME_ZONE, weekday: "short", day: "2-digit", month: "short", year: "numeric" });
const timeFmt = new Intl.DateTimeFormat("en-IN", { timeZone: CAMPUS_TIME_ZONE, hour: "2-digit", minute: "2-digit", hour12: false });
const longDateFmt = new Intl.DateTimeFormat("en-IN", { timeZone: CAMPUS_TIME_ZONE, weekday: "long", day: "numeric", month: "long", year: "numeric" });
const hourFmt = new Intl.DateTimeFormat("en-IN", { timeZone: CAMPUS_TIME_ZONE, hour: "numeric", hour12: false });

export function formatDate(iso: string | null | undefined): string {
  return iso ? dateFmt.format(new Date(iso)) : "";
}

export function formatTime(iso: string | null | undefined): string {
  return iso ? timeFmt.format(new Date(iso)) : "";
}

export function formatDateTime(iso: string | null | undefined): string {
  return iso ? `${formatDate(iso)}, ${formatTime(iso)}` : "";
}

export function formatLongDate(date: Date = new Date()): string {
  return longDateFmt.format(date);
}

export function greeting(date: Date = new Date()): string {
  const hour = Number(hourFmt.format(date));
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

export function initials(name: string): string {
  return name
    .replace(/^(Dr|Prof|Mr|Ms|Mrs)\.?\s+/i, "")
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");
}

export function titleCase(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function relativeTime(iso: string, now: Date = new Date()): string {
  const seconds = Math.round((now.getTime() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  return `${days} d ago`;
}
