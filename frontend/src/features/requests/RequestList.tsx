import { ClipboardList } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Badge, StatusBadge } from "@/components/ui/badge";
import { CellTitle, DataTable } from "@/components/ui/data-table";
import { Sheet } from "@/components/ui/sheet";
import { SkeletonTable } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { Tabs } from "@/components/ui/tabs";
import { WORKFLOW_STATUS, statusOf } from "@/components/dashboard/status";
import { RequestDetail } from "@/features/requests/WorkflowRequestCard";
import type { WorkflowRequest } from "@/types/api";
import { formatDateTime } from "@/utils/format";
import { rowKeys } from "@/utils/rowKeys";

const FILTERS: { id: string; label: string; match: (r: WorkflowRequest) => boolean }[] = [
  { id: "all", label: "All", match: () => true },
  { id: "draft", label: "Draft", match: (r) => r.status === "draft" },
  { id: "pending", label: "Pending", match: (r) => r.status === "pending" || r.status === "needs_review" },
  { id: "approved", label: "Approved", match: (r) => r.status === "approved" },
  { id: "rejected", label: "Rejected", match: (r) => r.status === "rejected" },
];

/**
 * The requester's own requests: status filter tabs, compact rows, and a detail drawer
 * with the workflow stepper. ``actions`` renders the drawer footer for one request.
 */
export function RequestList({
  requests,
  isLoading,
  error,
  onRetry,
  actions,
  empty,
}: {
  requests?: WorkflowRequest[];
  isLoading?: boolean;
  error?: Error | null;
  onRetry?: () => void;
  actions?: (request: WorkflowRequest, close: () => void) => ReactNode;
  empty: { title: string; description: string; action?: ReactNode };
}) {
  const [filter, setFilter] = useState("all");
  const [openId, setOpenId] = useState<string | null>(null);
  const active = FILTERS.find((f) => f.id === filter) ?? FILTERS[0];
  const rows = requests?.filter(active.match) ?? [];
  const open = requests?.find((r) => r.request_id === openId) ?? null;
  const close = () => setOpenId(null);
  const footer = open && actions ? actions(open, close) : null;

  return (
    <div>
      <Tabs label="Request status" value={filter} onChange={setFilter} tabs={FILTERS.map((f) => ({ id: f.id, label: f.label, count: requests ? requests.filter(f.match).length : undefined }))} />
      <div className="pt-5">
        {isLoading && <SkeletonTable rows={4} />}
        {error && <ErrorState message={`CampusNexus couldn't load your requests. ${error.message}`} onRetry={onRetry} />}
        {requests && requests.length === 0 && (
          <div className="rounded-card border border-border bg-surface">
            <EmptyState icon={<ClipboardList />} title={empty.title} description={empty.description} action={empty.action} />
          </div>
        )}
        {requests && requests.length > 0 && (
          <div className="overflow-hidden rounded-card border border-border bg-surface">
            {rows.length === 0 ? (
              <EmptyState compact title={`No ${active.label.toLowerCase()} requests`} description="You're all caught up." />
            ) : (
              <DataTable columns={["Request", "Reviewer", "Submitted", "Status"]} caption="Your requests">
                {rows.map((r) => (
                  <tr key={r.request_id} data-clickable="" tabIndex={0} aria-label={`${r.title} details`} onClick={() => setOpenId(r.request_id)} onKeyDown={rowKeys(() => setOpenId(r.request_id))}>
                    <td>
                      <CellTitle title={r.type_label} subtitle={r.context.event?.title ?? r.title} />
                    </td>
                    <td>{r.reviewer_name ?? <span className="text-muted">Not assigned</span>}</td>
                    <td className="text-sm whitespace-nowrap text-muted">{r.submitted_at ? formatDateTime(r.submitted_at) : "Not sent"}</td>
                    <td>
                      <StatusBadge {...statusOf(WORKFLOW_STATUS, r.status)} />
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
        title={open?.title ?? "Request"}
        description={open ? <Badge tone="primary">{open.type_label}</Badge> : undefined}
        footer={footer ?? undefined}
        className="sm:max-w-lg"
      >
        {open && (
          <div className="px-5 py-5">
            <RequestDetail request={open} viewer="student" />
          </div>
        )}
      </Sheet>
    </div>
  );
}
