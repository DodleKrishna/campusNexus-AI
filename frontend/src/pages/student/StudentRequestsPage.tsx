import { useMutation, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import { useState } from "react";
import { ApiError } from "@/api/client";
import { api, queryKeys } from "@/api/endpoints";
import { AskAgentLink } from "@/components/agents/AskAgentLink";
import { RequestsCard } from "@/components/dashboard/RequestsCard";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/states";
import { TabPanel, Tabs } from "@/components/ui/tabs";
import { RequestList } from "@/features/requests/RequestList";
import { useRequests, useWorkflowRequests } from "@/hooks/useStudentData";
import { PageTitle } from "@/pages/PageTitle";
import type { WorkflowRequest } from "@/types/api";

function WithdrawAction({ request, onDone }: { request: WorkflowRequest; onDone: () => void }) {
  const client = useQueryClient();
  const cancel = useMutation({
    mutationFn: () => api.cancelRequest(request.request_id),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.workflowRequests });
      onDone();
    },
  });
  return (
    <div className="space-y-2">
      {cancel.isError && <ErrorState message={cancel.error instanceof ApiError ? cancel.error.message : "That didn't work. Please try again."} />}
      <Button variant="danger" className="w-full" onClick={() => cancel.mutate()} disabled={cancel.isPending}>
        <X /> Withdraw request
      </Button>
    </div>
  );
}

export function StudentRequestsPage() {
  const { data, isLoading, error, refetch } = useWorkflowRequests();
  const approvals = useRequests();
  const [section, setSection] = useState("permissions");
  return (
    <div className="space-y-6">
      <PageTitle
        title="Requests"
        description="Permission, leave and on-duty requests you have sent, and actions awaiting approval."
        action={<AskAgentLink to="/student/agents/permission" label="Ask Permission Agent" />}
      />
      {approvals.data && approvals.data.length > 0 && (
        <Tabs
          label="Request type"
          value={section}
          onChange={setSection}
          tabs={[
            { id: "permissions", label: "Permission requests" },
            { id: "approvals", label: "Action approvals", count: approvals.data.length },
          ]}
        />
      )}
      {section === "approvals" ? (
        <TabPanel className="pt-0">
          <RequestsCard />
        </TabPanel>
      ) : (
        <RequestList
          requests={data}
          isLoading={isLoading}
          error={error as Error | null}
          onRetry={() => void refetch()}
          actions={(request, close) => (request.status === "pending" || request.status === "needs_review" ? <WithdrawAction request={request} onDone={close} /> : null)}
          empty={{
            title: "No permission requests yet",
            description: "Ask the Permission Agent. It prepares the request with your classes and attendance, and you confirm before it is sent.",
            action: <AskAgentLink to="/student/agents/permission" label="Ask Permission Agent" />,
          }}
        />
      )}
    </div>
  );
}
