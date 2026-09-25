import type { ReactNode } from "react";
import { StatusBadge, StatusDot } from "@/components/ui/badge";
import { Card, CardHeader } from "@/components/ui/card";
import { CellTitle, DataTable } from "@/components/ui/data-table";
import { SkeletonTable } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/ui/states";
import { CLASS_STATE, statusOf } from "@/components/dashboard/status";
import type { DepartmentClass } from "@/types/api";
import { cn } from "@/utils/cn";

const PULSE = [
  { label: "Active", states: ["active"], tone: "live" as const },
  { label: "Delayed", states: ["delayed", "due", "not_held"], tone: "caution" as const },
  { label: "Upcoming", states: ["upcoming"], tone: "neutral" as const },
  { label: "Completed", states: ["completed"], tone: "success" as const },
];

/** Active / Delayed / Upcoming / Completed counts for today's classes (states decided by the backend). */
export function DepartmentPulse({ classes, className }: { classes?: DepartmentClass[]; className?: string }) {
  return (
    <div className={cn("grid grid-cols-2 divide-border overflow-hidden rounded-card border border-border bg-surface sm:grid-cols-4 sm:divide-x", className)}>
      {PULSE.map((p, i) => {
        const count = classes?.filter((c) => p.states.includes(c.state)).length;
        return (
          <div key={p.label} className={cn("px-5 py-4", i < 2 && "max-sm:border-b max-sm:border-border", i % 2 === 0 && "max-sm:border-r max-sm:border-border")}>
            <p className="flex items-center gap-2 text-xs font-medium text-muted">
              <StatusDot tone={p.tone} pulse={p.tone === "live" && !!count} /> {p.label}
            </p>
            <p className={cn("mt-1 text-2xl font-semibold tabular-nums", p.tone === "caution" && count ? "text-caution-strong" : "text-ink")}>{count ?? "—"}</p>
          </div>
        );
      })}
    </div>
  );
}

/** Today's classes as one compact table: time, course, section, faculty, status. */
export function ActivityList({
  classes,
  graceMinutes,
  isLoading,
  title = "Today's activity",
  action,
  showFacultyTally = true,
}: {
  classes?: DepartmentClass[];
  graceMinutes?: number;
  isLoading?: boolean;
  title?: string;
  action?: ReactNode;
  showFacultyTally?: boolean;
}) {
  const order = ["active", "delayed", "due", "not_held", "upcoming", "completed", "cancelled"];
  const rows = [...(classes ?? [])].sort((a, b) => order.indexOf(a.state) - order.indexOf(b.state) || a.scheduled_start.localeCompare(b.scheduled_start));
  return (
    <Card className="overflow-hidden">
      <CardHeader title={title} description={`Times in IST${graceMinutes != null ? ` · delayed after a ${graceMinutes}-minute grace period` : ""}`} action={action} />
      {isLoading && !classes && <SkeletonTable rows={4} className="rounded-none border-x-0 border-b-0" />}
      {classes && classes.length === 0 && <EmptyState compact title="No classes today" description="None of these classes meets today." />}
      {rows.length > 0 && (
        <div className="border-t border-border">
          <DataTable columns={["Time", "Course", "Section", "Faculty", "Status"]} caption={title}>
            {rows.map((c) => (
              <tr key={`${c.assignment_id}-${c.scheduled_start}`}>
                <td className="font-medium whitespace-nowrap tabular-nums">
                  {c.start_local}–{c.end_local}
                </td>
                <td>
                  <CellTitle
                    title={c.course_title}
                    subtitle={
                      showFacultyTally && c.tally
                        ? `${c.room} · ${c.tally.present + c.tally.late}/${c.tally.roster} present`
                        : `${c.room}${c.is_extra_class ? " · extra class" : ""}`
                    }
                  />
                </td>
                <td className="whitespace-nowrap">{c.class_label}</td>
                <td>{c.faculty_name}</td>
                <td>
                  <StatusBadge {...statusOf(CLASS_STATE, c.state)} />
                </td>
              </tr>
            ))}
          </DataTable>
        </div>
      )}
    </Card>
  );
}
