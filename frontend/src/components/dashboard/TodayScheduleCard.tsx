import { Clock } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { SLOT } from "@/components/dashboard/status";
import { useTodaySchedule } from "@/hooks/useStudentData";
import { cn } from "@/utils/cn";

export function TodayScheduleCard() {
  const { data, isLoading, isError, error, refetch } = useTodaySchedule();
  return (
    <Card>
      <CardHeader
        icon={<Clock />}
        title="Today's schedule"
        description={data ? `${data.weekday} · times in IST` : "From your timetable"}
      />
      <CardBody>
        {isLoading && <SkeletonRows rows={2} />}
        {isError && <ErrorState message={`CampusNexus couldn't load your timetable. ${(error as Error).message}`} onRetry={() => void refetch()} />}
        {data && data.slots.length === 0 && <EmptyState title="No classes today" description={`Nothing on your timetable for ${data.weekday}.`} />}
        {data && data.slots.length > 0 && (
          <ol className="space-y-2">
            {data.slots.map((slot) => (
              <li
                key={`${slot.course_code}-${slot.start_time}`}
                className={cn(
                  "flex items-center gap-4 rounded-lg border px-4 py-3",
                  slot.status === "now" ? "border-accent/40 bg-accent-soft" : "border-border",
                  slot.status === "completed" && "opacity-70",
                )}
              >
                <div className="w-24 shrink-0 text-sm font-semibold tabular-nums">
                  {slot.start_time}–{slot.end_time}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium">{slot.course_title}</div>
                  <div className="truncate text-xs text-muted">
                    {slot.course_code} · {slot.location}
                    {slot.instructor ? ` · ${slot.instructor}` : ""}
                  </div>
                </div>
                <Badge tone={SLOT[slot.status].tone}>{SLOT[slot.status].label}</Badge>
              </li>
            ))}
          </ol>
        )}
      </CardBody>
    </Card>
  );
}
