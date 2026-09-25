import { ClipboardList } from "lucide-react";
import { StatusBadge } from "@/components/ui/badge";
import { Card, CardHeader } from "@/components/ui/card";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { REQUEST_STATUS, statusOf } from "@/components/dashboard/status";
import { useRequests } from "@/hooks/useStudentData";
import { formatDateTime } from "@/utils/format";

/** Actions requested through the student's missions and where their approval stands. */
export function RequestsCard({ limit }: { limit?: number }) {
  const { data, isLoading, isError, error, refetch } = useRequests();
  const items = limit ? data?.slice(0, limit) : data;
  return (
    <Card>
      <CardHeader title="Action approvals" description="Registrations and other actions awaiting or past approval" />
      {isLoading && <SkeletonRows rows={2} className="px-5 pb-5" />}
      {isError && (
        <div className="px-5 pb-5">
          <ErrorState message={`CampusNexus couldn't load your requests. ${(error as Error).message}`} onRetry={() => void refetch()} />
        </div>
      )}
      {items && items.length === 0 && (
        <EmptyState compact icon={<ClipboardList />} title="No action approvals" description="Registrations you ask an agent for appear here with their approval status." />
      )}
      {items && items.length > 0 && (
        <ul className="divide-y divide-border border-t border-border">
          {items.map((request) => (
            <li key={request.approval_id} className="flex items-start justify-between gap-3 px-5 py-3">
              <div className="min-w-0">
                <p className="line-clamp-2 text-sm text-ink">{request.title}</p>
                <p className="text-xs text-muted">Requested {formatDateTime(request.requested_at)}</p>
              </div>
              <StatusBadge {...statusOf(REQUEST_STATUS, request.status)} />
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
