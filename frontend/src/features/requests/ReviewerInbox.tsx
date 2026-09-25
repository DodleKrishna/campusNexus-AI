import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Inbox, Loader2, X } from "lucide-react";
import { useState } from "react";
import { ApiError } from "@/api/client";
import { api, queryKeys } from "@/api/endpoints";
import { StatusBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CellTitle, DataTable } from "@/components/ui/data-table";
import { Label, Textarea } from "@/components/ui/input";
import { Sheet } from "@/components/ui/sheet";
import { SkeletonTable } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { Tabs } from "@/components/ui/tabs";
import { WORKFLOW_STATUS, statusOf } from "@/components/dashboard/status";
import { RequestDetail } from "@/features/requests/WorkflowRequestCard";
import { requestHeading } from "@/features/requests/requestStatus";
import { useWorkflowRequests } from "@/hooks/useStudentData";
import type { WorkflowRequest } from "@/types/api";
import { formatDateTime } from "@/utils/format";
import { rowKeys } from "@/utils/rowKeys";

/** Approve / reject, fixed at the bottom of the request drawer. Only the routed reviewer reaches this. */
function DecisionBar({ request, onDone }: { request: WorkflowRequest; onDone: () => void }) {
  const client = useQueryClient();
  const [comment, setComment] = useState("");
  const decide = useMutation({
    mutationFn: (decision: "approve" | "reject") => api.decideRequest(request.request_id, decision, comment.trim() || undefined),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.workflowRequests });
      void client.invalidateQueries({ queryKey: queryKeys.facultyDashboard });
      void client.invalidateQueries({ queryKey: ["hod"] });
      void client.invalidateQueries({ queryKey: queryKeys.staffNotifications });
      onDone();
    },
  });
  const fieldId = `comment-${request.request_id}`;
  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        <Label htmlFor={fieldId} className="text-[13px] font-normal text-muted">
          Comment for the {request.requester_kind === "faculty" ? "faculty member" : "student"} (optional)
        </Label>
        <Textarea id={fieldId} rows={2} maxLength={1000} value={comment} onChange={(event) => setComment(event.target.value)} />
      </div>
      {decide.isError && <ErrorState message={decide.error instanceof ApiError ? decide.error.message : "That didn't work. Please try again."} />}
      <div className="grid grid-cols-2 gap-2">
        <Button variant="danger" onClick={() => decide.mutate("reject")} disabled={decide.isPending}>
          {decide.isPending && decide.variables === "reject" ? <Loader2 className="animate-spin" /> : <X />} Reject
        </Button>
        <Button onClick={() => decide.mutate("approve")} disabled={decide.isPending}>
          {decide.isPending && decide.variables === "approve" ? <Loader2 className="animate-spin" /> : <Check />} Approve
        </Button>
      </div>
    </div>
  );
}

/**
 * Requests routed to the signed-in reviewer (faculty, HOD or administration): tabs per
 * group, one compact table, and a right-side drawer with the decision fixed at its foot.
 */
export function ReviewerInbox({
  groups = [{ title: "Waiting for your decision", filter: () => true }],
}: {
  groups?: { title: string; filter: (request: WorkflowRequest) => boolean }[];
}) {
  const { data, isLoading, isError, error, refetch } = useWorkflowRequests();
  const [tab, setTab] = useState("0");
  const [openId, setOpenId] = useState<string | null>(null);
  const pending = data?.filter((r) => r.status === "pending") ?? [];
  const decided = data?.filter((r) => r.status !== "pending") ?? [];
  const tabs = [...groups.map((g, i) => ({ id: String(i), label: g.title, count: data ? pending.filter(g.filter).length : undefined })), { id: "decided", label: "Decided", count: data ? decided.length : undefined }];
  const rows = tab === "decided" ? decided : pending.filter(groups[Number(tab)]?.filter ?? (() => true));
  const open = data?.find((r) => r.request_id === openId) ?? null;
  const close = () => setOpenId(null);

  return (
    <div>
      <Tabs label="Request groups" value={tab} onChange={setTab} tabs={tabs} />
      <div className="pt-5">
        {isLoading && <SkeletonTable rows={4} />}
        {isError && <ErrorState message={`CampusNexus couldn't load requests. ${(error as Error).message}`} onRetry={() => void refetch()} />}
        {data && (
          <div className="overflow-hidden rounded-card border border-border bg-surface">
            {rows.length === 0 ? (
              <EmptyState icon={<Inbox />} title={tab === "decided" ? "No decided requests yet" : "No pending requests"} description={tab === "decided" ? "Requests you decide appear here." : "You're all caught up."} />
            ) : (
              <DataTable columns={["Requester", "Request", "Department", "Submitted", "Status", ""]} caption="Requests routed to you">
                {rows.map((r) => (
                  <tr key={r.request_id} data-clickable="" tabIndex={-1} onClick={() => setOpenId(r.request_id)} onKeyDown={rowKeys(() => setOpenId(r.request_id))}>
                    <td>
                      <CellTitle title={r.requester_name ?? r.student_name ?? "—"} subtitle={r.requester_kind === "faculty" ? "Faculty" : "Student"} />
                    </td>
                    <td>
                      <CellTitle title={r.type_label} subtitle={r.context.affected_classes.length ? `${r.context.affected_classes.length} affected class${r.context.affected_classes.length === 1 ? "" : "es"}` : r.context.event?.title} />
                    </td>
                    <td>{r.department_code ?? r.context.department_code ?? "—"}</td>
                    <td className="text-sm whitespace-nowrap text-muted">{formatDateTime(r.submitted_at ?? r.created_at)}</td>
                    <td>
                      <StatusBadge {...statusOf(WORKFLOW_STATUS, r.status)} />
                    </td>
                    <td>
                      <Button
                        variant={r.status === "pending" ? "outline" : "ghost"}
                        size="sm"
                        aria-label={`${r.status === "pending" ? "Review" : "View"} ${requestHeading(r, "faculty")}`}
                        onClick={(event) => {
                          event.stopPropagation();
                          setOpenId(r.request_id);
                        }}
                      >
                        {r.status === "pending" ? "Review" : "View"}
                      </Button>
                    </td>
                  </tr>
                ))}
              </DataTable>
            )}
          </div>
        )}
      </div>
      <Sheet
        open={open !== null}
        onClose={close}
        title={open ? requestHeading(open, "faculty") : "Request"}
        description={open?.type_label}
        className="sm:max-w-lg"
        footer={open && open.status === "pending" ? <DecisionBar key={open.request_id} request={open} onDone={close} /> : undefined}
      >
        {open && (
          <div className="px-5 py-5">
            <RequestDetail request={open} viewer="faculty" />
          </div>
        )}
      </Sheet>
    </div>
  );
}
