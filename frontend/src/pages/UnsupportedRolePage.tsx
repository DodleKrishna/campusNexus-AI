import { ShieldAlert } from "lucide-react";
import { ROLE_LABELS } from "@/auth/roles";
import { useAuth } from "@/auth/useAuth";
import { Logo } from "@/components/ui/brand";
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
    <div className="flex min-h-dvh flex-col items-center justify-center gap-8 bg-background px-4 py-10">
      <Logo />
      <Card className="w-full max-w-lg p-6">
        <div className="flex size-10 items-center justify-center rounded-lg bg-warning-soft text-warning">
          <ShieldAlert className="size-5" />
        </div>
        <h1 className="mt-4 text-base font-semibold text-ink">No workspace for the {ROLE_LABELS[user.role]} role</h1>
        <p className="mt-1 text-sm text-muted">
          You are signed in as {user.display_name} ({user.email}). CampusNexus has workspaces for students, faculty, heads of department and administrators only. Ask an
          administrator to change your role if you need access.
        </p>
        <Button className="mt-5" onClick={() => void logout()}>
          Sign out
        </Button>
      </Card>
    </div>
  );
}
