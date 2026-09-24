import { Bell } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { useNotifications } from "@/hooks/useStudentData";
import { relativeTime, titleCase } from "@/utils/format";

export function NotificationsCard({ limit = 4 }: { limit?: number }) {
  const { data, isLoading, isError, error, refetch } = useNotifications();
  return (
    <Card>
      <CardHeader icon={<Bell />} title="Notifications" />
      <CardBody>
        {isLoading && <SkeletonRows rows={3} />}
        {isError && <ErrorState message={`CampusNexus couldn't load notifications. ${(error as Error).message}`} onRetry={() => void refetch()} />}
        {data && data.length === 0 && <EmptyState title="You're all caught up" />}
        {data && data.length > 0 && (
          <ul className="space-y-3">
            {data.slice(0, limit).map((n) => (
              <li key={n.id}>
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium">{n.title}</span>
                  <Badge tone="neutral">{titleCase(n.category)}</Badge>
                </div>
                <p className="mt-0.5 line-clamp-2 text-xs text-muted">{n.body}</p>
                <p className="mt-0.5 text-[11px] text-subtle">{relativeTime(n.created_at)}</p>
              </li>
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}
