import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FilePlus2, Loader2, Send, X } from "lucide-react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ApiError } from "@/api/client";
import { api } from "@/api/endpoints";
import { Button } from "@/components/ui/button";
import { buttonVariants } from "@/components/ui/button-variants";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { PermissionAgentPanel } from "@/features/requests/PermissionAgentPanel";
import { WorkflowRequestCard } from "@/features/requests/WorkflowRequestCard";
import { useWorkflowRequests } from "@/hooks/useStudentData";
import { PageTitle } from "@/pages/PageTitle";
import type { WorkflowRequest } from "@/types/api";

function OwnRequestActions({ request }: { request: WorkflowRequest }) {
  const client = useQueryClient();
  const refresh = () => void client.invalidateQueries({ queryKey: ["workflow-requests"] });
  const send = useMutation({ mutationFn: () => api.submitRequest(request.request_id), onSuccess: refresh });
  const withdraw = useMutation({ mutationFn: () => api.cancelRequest(request.request_id), onSuccess: refresh });
  const error = send.error ?? withdraw.error;
  if (!["draft", "pending", "needs_review"].includes(request.status)) return null;
  return (
    <div className="space-y-2 border-t border-border pt-3">
      {error && <ErrorState message={error instanceof ApiError ? error.message : "That didn't work. Please try again."} />}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={() => withdraw.mutate()} disabled={send.isPending || withdraw.isPending}>
          <X /> {request.status === "draft" ? "Discard" : "Withdraw"}
        </Button>
        {request.status === "draft" && (
          <Button variant="accent" size="sm" onClick={() => send.mutate()} disabled={send.isPending || withdraw.isPending}>
            {send.isPending ? <Loader2 className="animate-spin" /> : <Send />} Confirm &amp; Send
          </Button>
        )}
      </div>
    </div>
  );
}

/** The faculty member's own requests to their HOD (drafts included). */
export function FacultyMyRequestsPage() {
  const { data, isLoading, isError, error, refetch } = useWorkflowRequests("mine");
  return (
    <div className="max-w-4xl space-y-6">
      <PageTitle
        title="My Requests"
        description="Leave, substitution, OD and department permission requests you have sent to your HOD."
        action={
          <Link to="/faculty/my-requests/new" className={buttonVariants({ variant: "accent" })}>
            <FilePlus2 /> Create Request
          </Link>
        }
      />
      {isLoading && <SkeletonRows rows={3} />}
      {isError && <ErrorState message={`CampusNexus couldn't load your requests. ${(error as Error).message}`} onRetry={() => void refetch()} />}
      {data && data.length === 0 && (
        <EmptyState title="No requests yet" description="Open the Permission Agent to prepare one. You confirm it before it is sent to your HOD." />
      )}
      {data?.map((request) => (
        <WorkflowRequestCard key={request.request_id} request={request} viewer="student" actions={<OwnRequestActions request={request} />} />
      ))}
    </div>
  );
}

/** Permission Agent for faculty; ?q= carries a request handed over from agent chat. */
export function FacultyNewRequestPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  return (
    <div className="max-w-4xl">
      <PermissionAgentPanel role="faculty" initialMessage={params.get("q") ?? undefined} onSent={() => navigate("/faculty/my-requests")} />
    </div>
  );
}
