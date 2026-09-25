import { CalendarDays } from "lucide-react";
import { Card, CardHeader } from "@/components/ui/card";
import { ScheduleTimeline } from "@/components/ui/schedule-timeline";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { SLOT } from "@/components/dashboard/status";
import { useTodaySchedule } from "@/hooks/useStudentData";

export function TodayScheduleCard() {
  const { data, isLoading, isError, error, refetch } = useTodaySchedule();
  return (
    <Card>
      <CardHeader title="Today's schedule" description={data ? `${data.weekday} · times in IST` : "From your timetable"} />
      {isLoading && <SkeletonRows rows={3} className="px-5 pb-5" />}
      {isError && (
        <div className="px-5 pb-5">
          <ErrorState message={`CampusNexus couldn't load your timetable. ${(error as Error).message}`} onRetry={() => void refetch()} />
        </div>
      )}
      {data && data.slots.length === 0 && <EmptyState compact icon={<CalendarDays />} title="No classes today" description={`Nothing on your timetable for ${data.weekday}.`} />}
      {data && data.slots.length > 0 && (
        <div className="border-t border-border py-1">
          <ScheduleTimeline
            items={data.slots.map((slot) => ({
              key: `${slot.course_code}-${slot.start_time}`,
              start: slot.start_time,
              end: slot.end_time,
              title: slot.course_title,
              subtitle: `${slot.location}${slot.instructor ? ` · ${slot.instructor}` : ""}`,
              status: SLOT[slot.status],
              current: slot.status === "now",
              muted: slot.status === "completed",
            }))}
          />
        </div>
      )}
    </Card>
  );
}
