import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FilePlus2, X } from "lucide-react";
import { Link } from "react-router-dom";
import { api, queryKeys } from "@/api/endpoints";
import { RequestsCard } from "@/components/dashboard/RequestsCard";
import { buttonVariants } from "@/components/ui/button-variants";
import { Button } from "@/components/ui/button";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { WorkflowRequestCard } from "@/features/requests/WorkflowRequestCard";
import { useWorkflowRequests } from "@/hooks/useStudentData";
import { PageTitle } from "@/pages/PageTitle";
import type { WorkflowRequest } from "@/types/api";

function CancelButton({ request }: { request: WorkflowRequest }) {
  const client = useQueryClient();
  const cancel = useMutation({
    mutationFn: () => api.cancelRequest(request.request_id),
    onSuccess: () => void client.invalidateQueries({ queryKey: queryKeys.workflowRequests }),
  });
  return (
    <div className="flex justify-end">
      <Button variant="ghost" size="sm" onClick={() => cancel.mutate()} disabled={cancel.isPending}>
        <X /> Withdraw request
      </Button>
    </div>
  );
}

export function StudentRequestsPage() {
  const { data, isLoading, isError, error, refetch } = useWorkflowRequests();
  return (
    <div className="max-w-4xl space-y-6">
      <PageTitle
        title="Requests"
        description="Permission, leave and on-duty requests you have sent, and actions awaiting approval."
        action={
          <Link to="/student/agents/permission" className={buttonVariants({ variant: "accent" })}>
            <FilePlus2 /> New Permission Request
          </Link>
        }
      />

      <section className="space-y-3">
        <h2 className="text-base font-semibold">Permission requests</h2>
        {isLoading && <SkeletonRows rows={3} />}
        {isError && <ErrorState message={`CampusNexus couldn't load your requests. ${(error as Error).message}`} onRetry={() => void refetch()} />}
        {data && data.length === 0 && (
          <EmptyState
            title="No permission requests yet"
            description="Ask the Permission Agent. It prepares the request with your classes and attendance, and you confirm before it is sent."
          />
        )}
        {data?.map((request) => (
          <WorkflowRequestCard
            key={request.request_id}
            request={request}
            viewer="student"
            actions={request.status === "pending" || request.status === "needs_review" ? <CancelButton request={request} /> : undefined}
          />
        ))}
      </section>

      <section className="space-y-3">
        <h2 className="text-base font-semibold">Action approvals</h2>
        <RequestsCard />
      </section>
    </div>
  );
}
