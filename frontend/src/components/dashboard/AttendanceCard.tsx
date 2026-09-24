import { BarChart3, ChevronRight } from "lucide-react";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { STANDING } from "@/components/dashboard/status";
import { useAttendance } from "@/hooks/useStudentData";
import type { CourseAttendance } from "@/types/api";
import { cn } from "@/utils/cn";

function Meter({ course }: { course: CourseAttendance }) {
  const pct = course.current_percentage ?? 0;
  const required = course.required_percentage;
  const tone = course.standing === "below_requirement" ? "bg-danger" : course.standing === "at_risk" ? "bg-warning" : "bg-success";
  return (
    <div className="relative h-1.5 w-full rounded-full bg-surface-muted" aria-hidden>
      <div className={cn("h-full rounded-full", tone)} style={{ width: `${Math.min(pct, 100)}%` }} />
      {required !== null && <div className="absolute -top-1 h-3.5 w-px bg-ink/50" style={{ left: `${required}%` }} />}
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between border-b border-dashed border-border py-2 text-sm last:border-0">
      <span className="text-muted">{label}</span>
      <span className="font-medium">{value}</span>
    </div>
  );
}

export function CourseAttendanceDetail({ course }: { course: CourseAttendance }) {
  const standing = STANDING[course.standing];
  const recovery =
    course.standing === "below_requirement"
      ? course.threshold_reachable === false
        ? "The requirement can no longer be reached this term."
        : `Attend the next ${course.classes_needed_to_reach_threshold} classes in a row to reach ${course.required_percentage}%.`
      : course.maximum_additional_absences_allowed !== null
        ? `You can miss up to ${course.maximum_additional_absences_allowed} more class${course.maximum_additional_absences_allowed === 1 ? "" : "es"} and stay at or above ${course.required_percentage}%.`
        : null;
  return (
    <div>
      <div className="flex items-start justify-between">
        <div>
          <div className="text-sm font-semibold">{course.course_title}</div>
          <div className="text-xs text-muted">
            {course.course_code}
            {course.instructor ? ` · ${course.instructor}` : ""}
          </div>
        </div>
        <Badge tone={standing.tone}>{standing.label}</Badge>
      </div>
      <div className="mt-4">
        <Row label="Current" value={course.current_percentage !== null ? `${course.current_percentage.toFixed(1)}%` : "—"} />
        <Row label="Attended / conducted" value={`${course.classes_attended} / ${course.classes_conducted}`} />
        <Row label="Required threshold" value={course.required_percentage !== null ? `${course.required_percentage}%` : "Not available"} />
      </div>
      {recovery && <p className="mt-3 rounded-lg bg-surface-muted px-3 py-2 text-sm">{recovery}</p>}
      {course.policy && (
        <p className="mt-3 text-xs text-muted">
          Source: {course.policy.title ?? course.policy.document_id}
          {course.policy.version ? ` (${course.policy.version})` : ""}
          {course.policy.section ? ` · ${course.policy.section}` : ""}
        </p>
      )}
    </div>
  );
}

export function AttendanceCard() {
  const { data, isLoading, isError, error, refetch } = useAttendance();
  const [open, setOpen] = useState<CourseAttendance | null>(null);
  const threshold = data?.find((c) => c.required_percentage !== null)?.required_percentage;
  return (
    <Card>
      <CardHeader icon={<BarChart3 />} title="Attendance overview" description={threshold ? `Required: ${threshold}% per course` : "Per enrolled course"} />
      <CardBody className="px-2 py-2">
        {isLoading && <div className="p-3"><SkeletonRows rows={4} /></div>}
        {isError && <ErrorState className="m-3" message={`CampusNexus couldn't load your attendance. ${(error as Error).message}`} onRetry={() => void refetch()} />}
        {data && data.length === 0 && <EmptyState className="m-3" title="No attendance records yet" />}
        {data && data.length > 0 && (
          <ul>
            {data.map((course) => {
              const standing = STANDING[course.standing];
              return (
                <li key={course.course_code}>
                  <button
                    type="button"
                    onClick={() => setOpen(course)}
                    className="grid w-full grid-cols-[minmax(0,1fr)_7rem_auto_1rem] items-center gap-4 rounded-lg px-3 py-3 text-left hover:bg-surface-muted"
                  >
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium">{course.course_title}</div>
                      <div className="text-xs text-muted">
                        {course.course_code} · {course.classes_attended}/{course.classes_conducted} classes
                      </div>
                    </div>
                    <div className="space-y-1.5">
                      <div className="text-right text-sm font-semibold tabular-nums">
                        {course.current_percentage !== null ? `${course.current_percentage.toFixed(1)}%` : "—"}
                      </div>
                      <Meter course={course} />
                    </div>
                    <Badge tone={standing.tone} className="justify-self-end">{standing.label}</Badge>
                    <ChevronRight className="size-4 text-subtle" />
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </CardBody>
      <Dialog open={open !== null} onClose={() => setOpen(null)} title="Course attendance">
        {open && <CourseAttendanceDetail course={open} />}
      </Dialog>
    </Card>
  );
}
