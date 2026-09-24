import { ReviewerInbox } from "@/features/requests/ReviewerInbox";
import { PageTitle } from "@/pages/PageTitle";

export function FacultyRequestsPage() {
  return (
    <div className="max-w-4xl space-y-6">
      <PageTitle title="Student Requests" description="Permission, leave and OD requests routed to you. Only you can decide these." />
      <ReviewerInbox />
    </div>
  );
}
