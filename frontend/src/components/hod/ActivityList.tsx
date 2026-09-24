import { Activity } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/ui/states";
import { CLASS_STATE } from "@/components/dashboard/status";
import type { DepartmentClass } from "@/types/api";
import { cn } from "@/utils/cn";
import { formatTime } from "@/utils/format";

/** Today's department classes with the state computed from timetable + attendance sessions. */
export function ActivityList({ classes, graceMinutes, isLoading }: { classes?: DepartmentClass[]; graceMinutes?: number; isLoading?: boolean }) {
  return (
    <Card>
      <CardHeader
        icon={<Activity />}
        title="Department activity"
        description={`Today's classes · times in IST${graceMinutes != null ? ` · "Not started" after a ${graceMinutes}-minute grace period` : ""}`}
      />
      <CardBody>
        {isLoading && <SkeletonRows rows={3} />}
        {classes && classes.length === 0 && <EmptyState title="No classes today" description="None of the department's classes meets today." />}
        {classes && classes.length > 0 && (
          <ol className="space-y-2">
            {classes.map((c) => {
              const state = CLASS_STATE[c.state] ?? { label: c.state, tone: "neutral" as const };
              return (
                <li
                  key={`${c.assignment_id}-${c.scheduled_start}`}
                  className={cn(
                    "flex flex-wrap items-center gap-4 rounded-lg border px-4 py-3",
                    c.state === "active" ? "border-accent/40 bg-accent-soft" : c.state === "delayed" || c.state === "not_held" ? "border-danger/25" : "border-border",
                  )}
                >
                  <div className="w-28 shrink-0 text-sm font-semibold tabular-nums">
                    {c.start_local}–{c.end_local}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="truncate text-sm font-medium">{c.course_title}</span>
                      <span className="text-xs text-muted">{c.class_label}</span>
                      {c.is_extra_class && <Badge tone="neutral">Extra class</Badge>}
                    </div>
                    <div className="truncate text-xs text-muted">
                      {c.faculty_name} · {c.room}
                      {c.actual_started_at && ` · started ${formatTime(c.actual_started_at)}`}
                      {c.tally && ` · ${c.tally.present + c.tally.late}/${c.tally.roster} present${c.tally.unmarked ? `, ${c.tally.unmarked} not marked` : ""}`}
                    </div>
                  </div>
                  <Badge tone={state.tone}>{state.label}</Badge>
                </li>
              );
            })}
          </ol>
        )}
      </CardBody>
    </Card>
  );
}
