import type { Role } from "@/types/api";

/**
 * Where each role lands after signing in. Every workspace role opens into the
 * Nexus assistant (CAMPUS AI); the role's classic workspace stays reachable
 * while it is replaced progressively.
 */
export const HOME_ROUTES: Record<Role, string> = {
  student: "/nexus",
  faculty: "/nexus",
  hod: "/nexus",
  admin: "/nexus",
  // STAFF has no workspace yet; it must never be treated as an administrator.
  staff: "/unsupported-role",
};

/** The pre-Nexus workspace for each role (mirrors the API's home_route). */
export const CLASSIC_ROUTES: Record<Role, string | null> = {
  student: "/student",
  faculty: "/faculty",
  hod: "/hod",
  admin: "/admin",
  staff: null,
};

export const NEXUS_ROLES: Role[] = ["student", "faculty", "hod", "admin"];

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

export function classicHomeFor(role: Role): string | null {
  return CLASSIC_ROUTES[role] ?? null;
}

/** Whether a path (e.g. the page a signed-out user was on) belongs to this role. */
export function isRouteForRole(role: Role, path: string): boolean {
  const roots = [homeRouteFor(role), classicHomeFor(role)].filter((r): r is string => Boolean(r));
  return roots.some((root) => path === root || path.startsWith(`${root}/`) || path.startsWith(`${root}?`));
}
