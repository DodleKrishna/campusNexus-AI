import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { FullPageLoader } from "@/components/ui/full-page-loader";
import { homeRouteFor } from "@/auth/roles";
import { useAuth } from "@/auth/useAuth";
import type { Role } from "@/types/api";

/**
 * Route guard. The server enforces every permission; this only keeps people
 * on screens that make sense for their role.
 */
export function RequireAuth({ roles, children }: { roles?: Role[]; children: ReactNode }) {
  const { status, user } = useAuth();
  const location = useLocation();

  if (status === "loading") return <FullPageLoader label="Restoring your session…" />;
  if (status === "anonymous" || !user) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  }
  if (roles && !roles.includes(user.role)) return <Navigate to={homeRouteFor(user.role)} replace />;
  return <>{children}</>;
}
