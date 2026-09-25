import { CalendarDays, ClipboardCheck, Eye, Loader2, Play } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/button";
import { buttonVariants } from "@/components/ui/button-variants";
import { Card, CardHeader } from "@/components/ui/card";
import { ScheduleTimeline } from "@/components/ui/schedule-timeline";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { SESSION, statusOf } from "@/components/dashboard/status";
import { classPath } from "@/components/faculty/paths";
import { useClassAction } from "@/hooks/useFacultyData";
import type { FacultyClass } from "@/types/api";
import { formatTime } from "@/utils/format";

function ClassAction({ item }: { item: FacultyClass }) {
  const navigate = useNavigate();
  const action = useClassAction(item.session_id);
  const busy = action.isPending;
  const error = action.error instanceof ApiError ? action.error.message : action.error ? "That didn't work. Please try again." : null;
  return (
    <>
      {item.status === "scheduled" && (
        <Button
          size="sm"
          disabled={!item.can_start || busy}
          title={item.start_blocked_reason ?? undefined}
          onClick={() => action.mutate({ kind: "start" }, { onSuccess: () => navigate(classPath(item)) })}
        >
          {busy ? <Loader2 className="animate-spin" /> : <Play />} Start Class
        </Button>
      )}
      {item.status === "active" && (
        <Link to={classPath(item)} className={buttonVariants({ size: "sm" })}>
          <ClipboardCheck /> Take attendance
        </Link>
      )}
      {item.status === "closed" && (
        <Link to={classPath(item)} className={buttonVariants({ variant: "ghost", size: "sm" })}>
          <Eye /> View
        </Link>
      )}
      {error && <span className="basis-full text-xs text-danger-strong">{error}</span>}
    </>
  );
}

function subtitle(item: FacultyClass): string {
  const { tally } = item;
  const base = `${item.department_code} ${item.year}-${item.section} · ${item.room}`;
  if (item.status === "active" || item.status === "closed") {
    return `${base} · ${item.actual_started_at ? `started ${formatTime(item.actual_started_at)} · ` : ""}${tally.roster - tally.unmarked}/${tally.roster} marked`;
  }
  if (item.status === "scheduled" && !item.can_start && item.start_blocked_reason) return `${base} · ${item.start_blocked_reason}`;
  return `${base} · ${tally.roster} students`;
}

export function TodayClassesCard({
  classes,
  isLoading,
  error,
  onRetry,
  title = "Today's teaching schedule",
}: {
  classes?: FacultyClass[];
  isLoading?: boolean;
  error?: Error | null;
  onRetry?: () => void;
  title?: string;
}) {
  return (
    <Card>
      <CardHeader title={title} description="Times in IST · a class can be started up to 15 minutes early" />
      {isLoading && !classes && <SkeletonRows rows={3} className="px-5 pb-5" />}
      {error && (
        <div className="px-5 pb-5">
          <ErrorState message={`CampusNexus couldn't load your classes. ${error.message}`} onRetry={onRetry} />
        </div>
      )}
      {classes && classes.length === 0 && <EmptyState compact icon={<CalendarDays />} title="No classes today" description="None of your teaching assignments meets today." />}
      {classes && classes.length > 0 && (
        <div className="border-t border-border py-1">
          <ScheduleTimeline
            items={classes.map((item) => ({
              key: String(item.session_id),
              start: item.start_local,
              end: item.end_local,
              title: (
                <Link to={classPath(item)} className="hover:underline">
                  {item.course_title}
                </Link>
              ),
              subtitle: subtitle(item),
              status: statusOf(SESSION, item.status),
              current: item.status === "active",
              muted: item.status === "closed" || item.status === "cancelled",
              action: <ClassAction item={item} />,
            }))}
          />
        </div>
      )}
    </Card>
  );
}
