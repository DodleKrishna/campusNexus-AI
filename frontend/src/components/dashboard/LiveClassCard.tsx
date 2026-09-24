import { Radio } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { SkeletonRows } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/states";
import { MARK } from "@/components/dashboard/status";
import { useLiveClass } from "@/hooks/useStudentData";
import { cn } from "@/utils/cn";
import { formatTime } from "@/utils/format";

const STATE: Record<string, { label: string; tone: "accent" | "info" | "neutral" | "danger" }> = {
  live: { label: "Live", tone: "accent" },
  scheduled: { label: "Scheduled", tone: "info" },
  closed: { label: "Ended", tone: "neutral" },
  cancelled: { label: "Cancelled", tone: "danger" },
};

/** The student's class right now, from the real attendance session (never inferred from the timetable). */
export function LiveClassCard() {
  const { data, isLoading, isError, error, refetch } = useLiveClass();
  const current = data?.current;
  const state = data ? STATE[data.state] : undefined;
  const mark = data?.my_attendance ? MARK[data.my_attendance] : undefined;
  return (
    <Card className={cn(data?.state === "live" && "border-accent/40")}>
      <CardHeader icon={<Radio />} title="Current class" description="Live from your faculty's attendance session" />
      <CardBody>
        {isLoading && <SkeletonRows rows={1} />}
        {isError && <ErrorState message={`CampusNexus couldn't load your current class. ${(error as Error).message}`} onRetry={() => void refetch()} />}
        {data && !current && (
          <div>
            <p className="text-sm font-medium">No class right now</p>
            <p className="mt-1 text-sm text-muted">{data.message}</p>
          </div>
        )}
        {data && current && state && (
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0 space-y-1">
              <div className="flex items-center gap-2">
                <Badge tone={state.tone}>
                  {data.state === "live" && <span className="size-1.5 animate-pulse rounded-full bg-current" />}
                  {state.label}
                </Badge>
                {current.is_extra_class && <Badge tone="neutral">Extra class</Badge>}
              </div>
              <p className="text-lg font-semibold">{current.course_title}</p>
              <p className="text-sm text-muted">
                {current.start_local}–{current.end_local} · {current.room} · {current.faculty_name}
              </p>
              <p className="text-sm">
                {data.state === "live" && current.actual_started_at && <>Started {formatTime(current.actual_started_at)}</>}
                {data.state === "scheduled" && "Faculty has not started the session yet"}
                {data.state === "closed" && current.actual_closed_at && <>Closed {formatTime(current.actual_closed_at)}</>}
                {data.state === "cancelled" && "This class was cancelled today"}
              </p>
            </div>
            {mark && (data.state === "live" || data.state === "closed") && (
              <div className="rounded-lg border border-border px-4 py-3 text-right">
                <p className="text-xs text-muted">Your attendance</p>
                <Badge tone={mark.tone} className="mt-1">
                  {mark.label}
                </Badge>
                {data.my_attendance_marked_at && <p className="mt-1 text-[11px] text-subtle">at {formatTime(data.my_attendance_marked_at)}</p>}
              </div>
            )}
          </div>
        )}
        {data?.next_class && current && (
          <p className="mt-3 border-t border-dashed border-border pt-3 text-xs text-muted">
            Next: {data.next_class.course_title} at {data.next_class.start_local} · {data.next_class.room}
          </p>
        )}
      </CardBody>
    </Card>
  );
}
