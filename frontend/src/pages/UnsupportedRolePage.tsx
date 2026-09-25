import { ShieldAlert } from "lucide-react";
import { ROLE_LABELS } from "@/auth/roles";
import { useAuth } from "@/auth/useAuth";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

/**
 * Signed-in accounts whose role has no workspace in this app (e.g. STAFF).
 * They are never routed into another role's workspace; the server would refuse them anyway.
 */
export function UnsupportedRolePage() {
  const { user, logout } = useAuth();
  if (!user) return null;
  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <Card className="max-w-lg px-6 py-6">
        <div className="flex items-start gap-4">
          <div className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-primary-soft text-primary">
            <ShieldAlert className="size-5" />
          </div>
          <div>
            <h1 className="text-base font-semibold">No workspace for the {ROLE_LABELS[user.role]} role</h1>
            <p className="mt-1 text-sm text-muted">
              You are signed in as {user.display_name} ({user.email}). CampusNexus has workspaces for students, faculty,
              heads of department and administrators only. Ask an administrator to change your role if you need access.
            </p>
            <Button className="mt-4" onClick={() => void logout()}>
              Sign out
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}
