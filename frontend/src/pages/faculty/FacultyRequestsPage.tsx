import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Loader2, X } from "lucide-react";
import { useState } from "react";
import { ApiError } from "@/api/client";
import { api, queryKeys } from "@/api/endpoints";
import { Button } from "@/components/ui/button";
import { Label, Textarea } from "@/components/ui/input";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { WorkflowRequestCard } from "@/features/requests/WorkflowRequestCard";
import { useWorkflowRequests } from "@/hooks/useStudentData";
import { PageTitle } from "@/pages/PageTitle";
import type { WorkflowRequest } from "@/types/api";

function DecisionPanel({ request }: { request: WorkflowRequest }) {
  const client = useQueryClient();
  const [comment, setComment] = useState("");
  const decide = useMutation({
    mutationFn: (decision: "approve" | "reject") => api.decideRequest(request.request_id, decision, comment.trim() || undefined),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.workflowRequests });
      void client.invalidateQueries({ queryKey: queryKeys.facultyDashboard });
    },
  });
  const fieldId = `comment-${request.request_id}`;
  return (
    <div className="space-y-2 border-t border-border pt-4">
      <Label htmlFor={fieldId} className="text-xs text-muted">
        Comment for the student (optional)
      </Label>
      <Textarea id={fieldId} rows={2} maxLength={1000} value={comment} onChange={(event) => setComment(event.target.value)} />
      {decide.isError && <ErrorState message={decide.error instanceof ApiError ? decide.error.message : "That didn't work. Please try again."} />}
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={() => decide.mutate("reject")} disabled={decide.isPending}>
          {decide.isPending && decide.variables === "reject" ? <Loader2 className="animate-spin" /> : <X />} Reject
        </Button>
        <Button variant="accent" onClick={() => decide.mutate("approve")} disabled={decide.isPending}>
          {decide.isPending && decide.variables === "approve" ? <Loader2 className="animate-spin" /> : <Check />} Approve
        </Button>
      </div>
    </div>
  );
}

export function FacultyRequestsPage() {
  const { data, isLoading, isError, error, refetch } = useWorkflowRequests();
  const pending = data?.filter((r) => r.status === "pending") ?? [];
  const decided = data?.filter((r) => r.status !== "pending") ?? [];
  return (
    <div className="max-w-4xl space-y-6">
      <PageTitle title="Student Requests" description="Permission, leave and OD requests routed to you. Only you can decide these." />
      {isLoading && <SkeletonRows rows={3} />}
      {isError && <ErrorState message={`CampusNexus couldn't load requests. ${(error as Error).message}`} onRetry={() => void refetch()} />}
      {data && (
        <section className="space-y-3">
          <h2 className="text-base font-semibold">Waiting for your decision ({pending.length})</h2>
          {pending.length === 0 && <EmptyState title="No pending requests" description="New requests routed to you appear here." />}
          {pending.map((request) => (
            <WorkflowRequestCard key={request.request_id} request={request} viewer="faculty" actions={<DecisionPanel request={request} />} />
          ))}
        </section>
      )}
      {decided.length > 0 && (
        <section className="space-y-3">
          <h2 className="text-base font-semibold">Decided</h2>
          {decided.map((request) => (
            <WorkflowRequestCard key={request.request_id} request={request} viewer="faculty" />
          ))}
        </section>
      )}
    </div>
  );
}
