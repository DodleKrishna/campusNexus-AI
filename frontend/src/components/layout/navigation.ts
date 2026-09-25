import {
  Bot,
  Briefcase,
  Building2,
  CalendarDays,
  ClipboardCheck,
  ClipboardList,
  Cpu,
  FileCheck2,
  GraduationCap,
  Inbox,
  LayoutDashboard,
  MessageSquareWarning,
  School,
  ScrollText,
  UserCog,
  Users,
  type LucideIcon,
} from "lucide-react";
import type { Role } from "@/types/api";

export interface NavItem {
  label: string;
  to: string;
  icon: LucideIcon;
  end?: boolean;
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
  { label: "Overview", to: "/admin", icon: LayoutDashboard, end: true },
  { label: "Departments", to: "/admin/departments", icon: Building2 },
  { label: "Users", to: "/admin/users", icon: UserCog },
  { label: "Attendance", to: "/admin/attendance", icon: ClipboardCheck },
  { label: "Requests", to: "/admin/requests", icon: Inbox },
  { label: "Complaints", to: "/admin/complaints", icon: MessageSquareWarning },
  { label: "AI Operations", to: "/admin/ai-operations", icon: Cpu },
  { label: "Audit Log", to: "/admin/audit", icon: ScrollText },
  { label: "Agents", to: "/admin/agents", icon: Bot },
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
