import { Activity, BookOpen, Bot, Boxes, Briefcase, Building2, CalendarDays, ClipboardCheck, ClipboardList, Cpu, FileCheck2, GraduationCap, Inbox, Landmark, LayoutDashboard, MessageSquareWarning, Plug, Radar, School, ScrollText, Sparkles, TrendingUp, UserCog, Users, Workflow, type LucideIcon } from "lucide-react";
import type { Role } from "@/types/api";

export interface NavItem {
  label: string;
  to: string;
  icon: LucideIcon;
  end?: boolean;
  /** Section header shown above the first item of a group (admin workspace). */
  section?: string;
}

const STUDENT_NAV: NavItem[] = [
  { label: "Overview", to: "/student", icon: LayoutDashboard, end: true },
  { label: "My Academics", to: "/student/academics", icon: GraduationCap },
  { label: "Agents", to: "/student/agents", icon: Bot, end: true },
  { label: "Requests", to: "/student/requests", icon: ClipboardList },
  { label: "Events", to: "/student/agents/events", icon: CalendarDays },
  { label: "Placements", to: "/student/agents/placements", icon: Briefcase },
  { label: "Complaints", to: "/student/agents/complaints", icon: MessageSquareWarning },
];

const FACULTY_NAV: NavItem[] = [
  { label: "Overview", to: "/faculty", icon: LayoutDashboard, end: true },
  { label: "My Classes", to: "/faculty/classes", icon: School },
  { label: "Attendance", to: "/faculty/attendance", icon: ClipboardCheck },
  { label: "Agents", to: "/faculty/agents", icon: Bot },
  { label: "Student Requests", to: "/faculty/requests", icon: Inbox },
  { label: "My Requests", to: "/faculty/my-requests", icon: FileCheck2 },
];

const HOD_NAV: NavItem[] = [
  { label: "Overview", to: "/hod", icon: LayoutDashboard, end: true },
  { label: "Department", to: "/hod/department", icon: Building2 },
  { label: "Faculty", to: "/hod/faculty", icon: Users },
  { label: "Students", to: "/hod/students", icon: GraduationCap },
  { label: "Attendance", to: "/hod/attendance", icon: ClipboardCheck },
  { label: "Agents", to: "/hod/agents", icon: Bot },
  { label: "Requests", to: "/hod/requests", icon: Inbox },
  { label: "My Requests", to: "/hod/my-requests", icon: FileCheck2 },
  { label: "Complaints", to: "/hod/complaints", icon: MessageSquareWarning },
];

const ADMIN_NAV: NavItem[] = [
  { label: "Command Center", to: "/admin", icon: LayoutDashboard, end: true, section: "Overview" },
  { label: "Institution", to: "/admin/institution", icon: Landmark, section: "Overview" },
  { label: "Workflows", to: "/admin/workflows", icon: Workflow, section: "Operations" },
  { label: "Monitors", to: "/admin/monitors", icon: Radar, section: "Operations" },
  { label: "Requests", to: "/admin/requests", icon: Inbox, section: "Operations" },
  { label: "Attendance", to: "/admin/attendance", icon: ClipboardCheck, section: "Operations" },
  { label: "Complaints", to: "/admin/complaints", icon: MessageSquareWarning, section: "Operations" },
  { label: "Agent Catalog", to: "/admin/agent-catalog", icon: Boxes, section: "AI Workforce" },
  { label: "Deployed Agents", to: "/admin/deployed-agents", icon: Bot, section: "AI Workforce" },
  { label: "AI Operations", to: "/admin/ai-operations", icon: Cpu, section: "AI Workforce" },
  { label: "Control Tower", to: "/admin/control-tower", icon: Activity, section: "AI Workforce" },
  { label: "Agents", to: "/admin/agents", icon: Sparkles, section: "AI Workforce" },
  { label: "Connectors", to: "/admin/connectors", icon: Plug, section: "Enterprise" },
  { label: "Knowledge", to: "/admin/knowledge", icon: BookOpen, section: "Enterprise" },
  { label: "Audit Log", to: "/admin/audit", icon: ScrollText, section: "Enterprise" },
  { label: "Institution Value", to: "/admin/value", icon: TrendingUp, section: "Enterprise" },
  { label: "Users", to: "/admin/users", icon: UserCog, section: "Admin" },
  { label: "Departments", to: "/admin/departments", icon: Building2, section: "Admin" },
];

export const WORKSPACE_LABELS: Record<Role, string> = {
  student: "Student workspace",
  faculty: "Faculty workspace",
  hod: "HOD workspace",
  admin: "Admin workspace",
  staff: "Staff",
};

export function navigationFor(role: Role): NavItem[] {
  if (role === "student") return STUDENT_NAV;
  if (role === "faculty") return FACULTY_NAV;
  if (role === "hod") return HOD_NAV;
  if (role === "admin") return ADMIN_NAV;
  return [];
}

export function settingsPathFor(role: Role): string {
  const home = navigationFor(role)[0];
  return home ? `${home.to}/settings` : "/";
}

/** Where "Ask CampusNexus" sends a question: the role's own read-only Enquiry Agent. */
export function enquiryPathFor(role: Role): string | null {
  const home = navigationFor(role)[0];
  return home ? `${home.to}/agents/enquiry` : null;
}

/** The section title shown in the top bar for the current path. */
export function sectionTitleFor(role: Role, pathname: string): string {
  if (pathname.endsWith("/settings")) return "Settings";
  const items = navigationFor(role);
  const exact = items.find((item) => item.to === pathname);
  if (exact) return exact.label;
  const prefix = items
    .filter((item) => pathname.startsWith(`${item.to}/`))
    .sort((a, b) => b.to.length - a.to.length)[0];
  if (prefix) return prefix.label;
  if (pathname.includes("/agents")) return "Agents";
  return items[0]?.label ?? "";
}
