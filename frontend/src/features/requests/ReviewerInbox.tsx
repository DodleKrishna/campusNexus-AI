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
import type { WorkflowRequest } from "@/types/api";

function DecisionPanel({ request }: { request: WorkflowRequest }) {
  const client = useQueryClient();
  const [comment, setComment] = useState("");
  const decide = useMutation({
    mutationFn: (decision: "approve" | "reject") => api.decideRequest(request.request_id, decision, comment.trim() || undefined),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.workflowRequests });
      void client.invalidateQueries({ queryKey: queryKeys.facultyDashboard });
      void client.invalidateQueries({ queryKey: ["hod"] });
      void client.invalidateQueries({ queryKey: queryKeys.staffNotifications });
    },
  });
  const fieldId = `comment-${request.request_id}`;
  return (
    <div className="space-y-2 border-t border-border pt-4">
      <Label htmlFor={fieldId} className="text-xs text-muted">
        Comment for the {request.requester_kind === "faculty" ? "faculty member" : "student"} (optional)
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

/** Requests routed to the signed-in reviewer (faculty or HOD), optionally split into groups. */
export function ReviewerInbox({
  groups = [{ title: "Waiting for your decision", filter: () => true }],
}: {
  groups?: { title: string; filter: (request: WorkflowRequest) => boolean }[];
}) {
  const { data, isLoading, isError, error, refetch } = useWorkflowRequests();
  const pending = data?.filter((r) => r.status === "pending") ?? [];
  const decided = data?.filter((r) => r.status !== "pending") ?? [];
  return (
    <div className="space-y-6">
      {isLoading && <SkeletonRows rows={3} />}
      {isError && <ErrorState message={`CampusNexus couldn't load requests. ${(error as Error).message}`} onRetry={() => void refetch()} />}
      {data &&
        groups.map((group) => {
          const items = pending.filter(group.filter);
          return (
            <section key={group.title} className="space-y-3">
              <h2 className="text-base font-semibold">
                {group.title} ({items.length})
              </h2>
              {items.length === 0 && <EmptyState title="Nothing waiting" description="New requests routed to you appear here." />}
              {items.map((request) => (
                <WorkflowRequestCard key={request.request_id} request={request} viewer="faculty" actions={<DecisionPanel request={request} />} />
              ))}
            </section>
          );
        })}
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
