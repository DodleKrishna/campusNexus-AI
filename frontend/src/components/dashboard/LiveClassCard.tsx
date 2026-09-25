import { Clock, MapPin, UserRound } from "lucide-react";
import { StatusBadge } from "@/components/ui/badge";
import { Card, CardHeader } from "@/components/ui/card";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { MARK, type StatusInfo } from "@/components/dashboard/status";
import { useLiveClass } from "@/hooks/useStudentData";
import { cn } from "@/utils/cn";
import { formatTime } from "@/utils/format";

const STATE: Record<string, StatusInfo> = {
  live: { label: "Live", tone: "live", pulse: true },
  scheduled: { label: "Not started", tone: "caution" },
  closed: { label: "Ended", tone: "neutral" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

/** The student's class right now, from the real attendance session (never inferred from the timetable). */
export function LiveClassCard() {
  const { data, isLoading, isError, error, refetch } = useLiveClass();
  const current = data?.current;
  const state = data ? STATE[data.state] : undefined;
  const mark = data?.my_attendance ? MARK[data.my_attendance] : undefined;
  return (
    <Card className={cn("h-full", data?.state === "live" && "border-primary/40")}>
      <CardHeader title={current ? "Current class" : "Next class"} action={state && current ? <StatusBadge {...state} /> : undefined} />
      {isLoading && <SkeletonRows rows={2} className="px-5 pb-5" />}
      {isError && (
        <div className="px-5 pb-5">
          <ErrorState message={`CampusNexus couldn't load your current class. ${(error as Error).message}`} onRetry={() => void refetch()} />
        </div>
      )}
      {data && !current && !data.next_class && <EmptyState compact icon={<Clock />} title="No class right now" description={data.message} />}
      {data && !current && data.next_class && (
        <div className="px-5 pb-5">
          <p className="text-base font-semibold text-ink">{data.next_class.course_title}</p>
          <p className="mt-1 text-sm text-muted">
            Starts at {data.next_class.start_local} · {data.next_class.room}
          </p>
        </div>
      )}
      {data && current && state && (
        <div className="px-5 pb-5">
          <p className="text-base font-semibold text-ink">{current.course_title}</p>
          <ul className="mt-2 space-y-1 text-[13px] text-muted [&_svg]:size-3.5 [&_svg]:text-subtle">
            <li className="flex items-center gap-2">
              <Clock /> {current.start_local}–{current.end_local}
            </li>
            <li className="flex items-center gap-2">
              <MapPin /> {current.room}
            </li>
            <li className="flex items-center gap-2">
              <UserRound /> {current.faculty_name}
            </li>
          </ul>
          <p className="mt-3 text-sm text-ink">
            {data.state === "live" && current.actual_started_at && <>Started {formatTime(current.actual_started_at)}</>}
            {data.state === "scheduled" && "Your faculty has not started the session yet"}
            {data.state === "closed" && current.actual_closed_at && <>Closed {formatTime(current.actual_closed_at)}</>}
            {data.state === "cancelled" && "This class was cancelled today"}
          </p>
          {mark && (data.state === "live" || data.state === "closed") && (
            <div className="mt-3 flex items-center justify-between gap-3 border-t border-border pt-3">
              <span className="text-[13px] text-muted">
                Your attendance{data.my_attendance_marked_at ? ` · ${formatTime(data.my_attendance_marked_at)}` : ""}
              </span>
              <StatusBadge {...mark} />
            </div>
          )}
          {data.next_class && (
            <p className="mt-3 border-t border-border pt-3 text-[13px] text-muted">
              Next: <span className="font-medium text-ink">{data.next_class.course_title}</span> at {data.next_class.start_local}
            </p>
          )}
        </div>
      )}
    </Card>
  );
}
