import type { Role } from "@/types/api";

/** Where each role lands after signing in (mirrors the API's home_route). */
export const HOME_ROUTES: Record<Role, string> = {
  student: "/student",
  faculty: "/faculty",
  hod: "/hod",
  admin: "/admin",
  staff: "/admin",
};

export const ROLE_LABELS: Record<Role, string> = {
  student: "Student",
  faculty: "Faculty",
  hod: "Head of Department",
  admin: "Administrator",
  staff: "Staff",
};

export function homeRouteFor(role: Role): string {
  return HOME_ROUTES[role] ?? "/login";
}
