import { ROLE_LABELS } from "@/auth/roles";
import { useAuth } from "@/auth/useAuth";
import { WORKSPACE_LABELS } from "@/components/layout/navigation";
import { ModeBadge } from "@/components/layout/ModeBadge";
import { Button } from "@/components/ui/button";
import { DetailList, DetailRow } from "@/components/ui/detail-list";
import { FormSection } from "@/components/ui/form-section";
import { useHealth } from "@/hooks/useStudentData";
import { PageTitle } from "@/pages/PageTitle";

export function SettingsPage() {
  const { user, logout } = useAuth();
  const { data: health } = useHealth();
  if (!user) return null;
  return (
    <div className="max-w-4xl space-y-6">
      <PageTitle title="Settings" description="Your account and how this CampusNexus instance is running." />
      <div>
        <FormSection title="Account" description="Managed by your institution. Contact an administrator to change these details.">
          <DetailList>
            <DetailRow label="Name">{user.display_name}</DetailRow>
            <DetailRow label="Email">{user.email}</DetailRow>
            <DetailRow label="Role">{ROLE_LABELS[user.role]}</DetailRow>
            {user.student_id && <DetailRow label="Student ID">{user.student_id}</DetailRow>}
          </DetailList>
        </FormSection>
        <FormSection title="Workspace">
          <DetailList>
            <DetailRow label="Workspace">{WORKSPACE_LABELS[user.role]}</DetailRow>
            {user.department_name && <DetailRow label="Department">{user.department_name}</DetailRow>}
          </DetailList>
        </FormSection>
        {health && (
          <FormSection title="System information" description="Deterministic output is never presented as live AI.">
            <DetailList>
              <DetailRow label="AI mode">
                <ModeBadge />
              </DetailRow>
              <DetailRow label="Provider">{health.llm.provider}</DetailRow>
              {health.llm.model && <DetailRow label="Model">{health.llm.model}</DetailRow>}
            </DetailList>
          </FormSection>
        )}
        <FormSection title="Session">
          <div className="flex items-center justify-between gap-4 py-3">
            <p className="text-sm text-muted">Sign out of CampusNexus on this device.</p>
            <Button variant="danger" size="sm" onClick={() => void logout()}>
              Sign out
            </Button>
          </div>
        </FormSection>
      </div>
    </div>
  );
}
