import { RequestsCard } from "@/components/dashboard/RequestsCard";
import { PageTitle } from "@/pages/PageTitle";

export function StudentRequestsPage() {
  return (
    <div className="max-w-3xl space-y-6">
      <PageTitle
        title="Requests"
        description="Actions prepared for you (such as event registrations) and where each approval stands. Leave and permission requests arrive in a later phase."
      />
      <RequestsCard />
    </div>
  );
}
