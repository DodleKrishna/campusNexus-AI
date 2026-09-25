import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Send, X } from "lucide-react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { ApiError } from "@/api/client";
import { api } from "@/api/endpoints";
import { AskAgentLink } from "@/components/agents/AskAgentLink";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/states";
import { PermissionAgentPanel } from "@/features/requests/PermissionAgentPanel";
import { RequestList } from "@/features/requests/RequestList";
import { useWorkflowRequests } from "@/hooks/useStudentData";
import { PageTitle } from "@/pages/PageTitle";
import type { WorkflowRequest } from "@/types/api";

function OwnRequestActions({ request, onDone }: { request: WorkflowRequest; onDone: () => void }) {
  const client = useQueryClient();
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["workflow-requests"] });
    onDone();
  };
  const send = useMutation({ mutationFn: () => api.submitRequest(request.request_id), onSuccess: refresh });
  const withdraw = useMutation({ mutationFn: () => api.cancelRequest(request.request_id), onSuccess: refresh });
  const error = send.error ?? withdraw.error;
  if (!["draft", "pending", "needs_review"].includes(request.status)) return null;
  return (
    <div className="space-y-2">
      {error && <ErrorState message={error instanceof ApiError ? error.message : "That didn't work. Please try again."} />}
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <Button variant="danger" onClick={() => withdraw.mutate()} disabled={send.isPending || withdraw.isPending} className={request.status === "draft" ? "" : "sm:col-span-2"}>
          <X /> {request.status === "draft" ? "Discard" : "Withdraw"}
        </Button>
        {request.status === "draft" && (
          <Button onClick={() => send.mutate()} disabled={send.isPending || withdraw.isPending}>
            {send.isPending ? <Loader2 className="animate-spin" /> : <Send />} Confirm &amp; Send
          </Button>
        )}
      </div>
    </div>
  );
}

const baseOf = (pathname: string) => (pathname.startsWith("/hod") ? "/hod" : "/faculty");

/** The signed-in faculty member's (or HOD's) own requests (drafts included). */
export function FacultyMyRequestsPage() {
  const base = baseOf(useLocation().pathname);
  const { data, isLoading, error, refetch } = useWorkflowRequests("mine");
  const reviewer = base === "/hod" ? "the Administration" : "your HOD";
  return (
    <div className="space-y-6">
      <PageTitle
        title="My Requests"
        description={
          base === "/hod"
            ? "Leave, resource, permission and escalation requests you have sent to the Administration."
            : "Leave, substitution, OD and department permission requests you have sent to your HOD."
        }
        action={<AskAgentLink to={`${base}/my-requests/new`} label="Ask Permission Agent" />}
      />
      <RequestList
        requests={data}
        isLoading={isLoading}
        error={error as Error | null}
        onRetry={() => void refetch()}
        actions={(request, close) => <OwnRequestActions request={request} onDone={close} />}
        empty={{
          title: "No requests yet",
          description: `Ask the Permission Agent to prepare one. You confirm it before it is sent to ${reviewer}.`,
          action: <AskAgentLink to={`${base}/my-requests/new`} label="Ask Permission Agent" />,
        }}
      />
    </div>
  );
}

/** Permission Agent for faculty or an HOD; ?q= carries a request handed over from agent chat. */
export function FacultyNewRequestPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const base = baseOf(useLocation().pathname);
  return (
    <div className="mx-auto w-full max-w-4xl space-y-4">
      <Link to={`${base}/my-requests`} className="inline-flex items-center gap-1.5 text-sm text-muted transition-colors hover:text-ink">
        ← My requests
      </Link>
      <PermissionAgentPanel role={base === "/hod" ? "hod" : "faculty"} initialMessage={params.get("q") ?? undefined} onSent={() => navigate(`${base}/my-requests`)} />
    </div>
  );
}
