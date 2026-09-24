import { Bot, Briefcase, CalendarDays, ClipboardList, GraduationCap, LayoutDashboard, MessageSquareWarning, type LucideIcon } from "lucide-react";
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

export function navigationFor(role: Role): NavItem[] {
  if (role === "student") return STUDENT_NAV;
  const home = role === "hod" ? "/hod" : role === "faculty" ? "/faculty" : "/admin";
  return [{ label: "Overview", to: home, icon: LayoutDashboard, end: true }];
}

export function settingsPathFor(role: Role): string {
  return role === "student" ? "/student/settings" : `${navigationFor(role)[0].to}/settings`;
}
