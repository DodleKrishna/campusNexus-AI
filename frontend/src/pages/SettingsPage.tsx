import { ROLE_LABELS } from "@/auth/roles";
import { useAuth } from "@/auth/useAuth";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { useHealth } from "@/hooks/useStudentData";
import { PageTitle } from "@/pages/PageTitle";

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between border-b border-dashed border-border py-2.5 text-sm last:border-0">
      <span className="text-muted">{label}</span>
      <span className="font-medium">{value}</span>
    </div>
  );
}

export function SettingsPage() {
  const { user } = useAuth();
  const { data: health } = useHealth();
  if (!user) return null;
  return (
    <div className="max-w-2xl space-y-6">
      <PageTitle title="Settings" description="Your account and how this CampusNexus instance is running." />
      <Card>
        <CardHeader title="Account" />
        <CardBody>
          <Row label="Name" value={user.display_name} />
          <Row label="Email" value={user.email} />
          <Row label="Role" value={ROLE_LABELS[user.role]} />
          {user.student_id && <Row label="Student ID" value={user.student_id} />}
          {user.department_name && <Row label="Department" value={user.department_name} />}
        </CardBody>
      </Card>
      {health && (
        <Card>
          <CardHeader title="AI mode" />
          <CardBody>
            <Row label="Mode" value={health.llm.live ? "Live AI" : "Local / deterministic (no external AI)"} />
            <Row label="Provider" value={health.llm.provider} />
            {health.llm.model && <Row label="Model" value={health.llm.model} />}
          </CardBody>
        </Card>
      )}
    </div>
  );
}
