import { Construction } from "lucide-react";
import { ROLE_LABELS } from "@/auth/roles";
import { useAuth } from "@/auth/useAuth";
import { Card } from "@/components/ui/card";

/** Faculty, HOD and Admin sign in to a real shell; their dashboards arrive in the next phase. */
export function RoleWorkspacePage() {
  const { user } = useAuth();
  if (!user) return null;
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Welcome, {user.display_name}</h1>
        <p className="mt-1 text-sm text-muted">
          {ROLE_LABELS[user.role]}
          {user.department_name ? ` · ${user.department_name}` : ""}
        </p>
      </header>
      <Card className="flex items-start gap-4 px-6 py-6">
        <div className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-primary-soft text-primary">
          <Construction className="size-5" />
        </div>
        <div>
          <h2 className="text-base font-semibold">Your {ROLE_LABELS[user.role]} workspace is coming in the next phase</h2>
          <p className="mt-1 max-w-2xl text-sm text-muted">
            Your account and sign-in are set up. Class, department and approval tools for your role are being built next.
            Until then, the approval Action Center is available in the CampusNexus debug console.
          </p>
        </div>
      </Card>
    </div>
  );
}
