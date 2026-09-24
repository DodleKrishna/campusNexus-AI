import { ClipboardList } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { REQUEST_STATUS } from "@/components/dashboard/status";
import { useRequests } from "@/hooks/useStudentData";
import { formatDateTime } from "@/utils/format";

/** Actions requested through the student's missions and where their approval stands. */
export function RequestsCard({ limit }: { limit?: number }) {
  const { data, isLoading, isError, error, refetch } = useRequests();
  const items = limit ? data?.slice(0, limit) : data;
  return (
    <Card>
      <CardHeader icon={<ClipboardList />} title="My requests" description="Actions awaiting or past approval" />
      <CardBody>
        {isLoading && <SkeletonRows rows={2} />}
        {isError && <ErrorState message={`CampusNexus couldn't load your requests. ${(error as Error).message}`} onRetry={() => void refetch()} />}
        {items && items.length === 0 && (
          <EmptyState title="No requests yet" description="Registrations and other actions you ask for appear here with their approval status." />
        )}
        {items && items.length > 0 && (
          <ul className="space-y-3">
            {items.map((request) => {
              const status = REQUEST_STATUS[request.status] ?? { label: request.status, tone: "neutral" as const };
              return (
                <li key={request.approval_id} className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="line-clamp-2 text-sm">{request.title}</div>
                    <div className="text-xs text-muted">Requested {formatDateTime(request.requested_at)}</div>
                  </div>
                  <Badge tone={status.tone}>{status.label}</Badge>
                </li>
              );
            })}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}
