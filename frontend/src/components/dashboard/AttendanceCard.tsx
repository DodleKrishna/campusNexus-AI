import { useState } from "react";
import { rowKeys } from "@/utils/rowKeys";
import { StatusBadge } from "@/components/ui/badge";
import { CellTitle, DataTable } from "@/components/ui/data-table";
import { DetailList, DetailRow } from "@/components/ui/detail-list";
import { ProgressBar } from "@/components/ui/progress";
import { Sheet } from "@/components/ui/sheet";
import { SkeletonTable } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { STANDING } from "@/components/dashboard/status";
import { useAttendance } from "@/hooks/useStudentData";
import type { CourseAttendance } from "@/types/api";

const TONE = { below_requirement: "danger", at_risk: "warning", good: "success", unknown: "accent" } as const;

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
    <div className="space-y-6">
      <div>
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="text-3xl font-semibold tracking-tight tabular-nums">{course.current_percentage !== null ? `${course.current_percentage.toFixed(1)}%` : "—"}</p>
            <p className="text-xs font-medium tracking-wide text-muted uppercase">Current attendance</p>
          </div>
          <StatusBadge {...standing} />
        </div>
        <ProgressBar
          className="mt-4"
          value={course.current_percentage ?? 0}
          marker={course.required_percentage}
          tone={TONE[course.standing]}
          label={`${course.course_title} attendance`}
        />
      </div>
      <section>
        <h3 className="text-xs font-semibold tracking-wide text-muted uppercase">Course</h3>
        <DetailList className="mt-1">
          <DetailRow label="Classes attended">
            {course.classes_attended} / {course.classes_conducted}
          </DetailRow>
          <DetailRow label="Required">{course.required_percentage !== null ? `${course.required_percentage}%` : "Not available"}</DetailRow>
          {course.instructor && <DetailRow label="Instructor">{course.instructor}</DetailRow>}
        </DetailList>
      </section>
      {recovery && (
        <section>
          <h3 className="text-xs font-semibold tracking-wide text-muted uppercase">{course.standing === "below_requirement" ? "Recovery" : "Margin"}</h3>
          <p className="mt-2 text-sm text-ink">{recovery}</p>
        </section>
      )}
      {course.policy && (
        <section>
          <h3 className="text-xs font-semibold tracking-wide text-muted uppercase">Policy</h3>
          <p className="mt-2 text-sm text-ink">
            {course.policy.title ?? course.policy.document_id}
            {course.policy.version ? ` (${course.policy.version})` : ""}
          </p>
          {course.policy.section && <p className="text-[13px] text-muted">{course.policy.section}</p>}
        </section>
      )}
    </div>
  );
}

/** Per-course attendance as a table; a row opens its details in a side drawer. */
export function AttendanceTable() {
  const { data, isLoading, isError, error, refetch } = useAttendance();
  const [open, setOpen] = useState<CourseAttendance | null>(null);
  if (isLoading) return <SkeletonTable rows={4} />;
  if (isError) return <ErrorState message={`CampusNexus couldn't load your attendance. ${(error as Error).message}`} onRetry={() => void refetch()} />;
  if (!data || data.length === 0) return <EmptyState title="No attendance records yet" description="Attendance appears after your first recorded class." />;
  return (
    <>
      <div className="overflow-hidden rounded-card border border-border bg-surface">
        <DataTable columns={["Course", "Attendance", "Attended", "Conducted", "Status"]} caption="Course attendance">
          {data.map((course) => (
            <tr
              key={course.course_code}
              data-clickable=""
              tabIndex={0}
              aria-label={`${course.course_title} attendance details`}
              onClick={() => setOpen(course)}
              onKeyDown={rowKeys(() => setOpen(course))}
            >
              <td>
                <CellTitle title={course.course_title} subtitle={course.course_code} />
              </td>
              <td className="font-semibold tabular-nums">{course.current_percentage !== null ? `${course.current_percentage.toFixed(1)}%` : "—"}</td>
              <td className="tabular-nums">{course.classes_attended}</td>
              <td className="tabular-nums">{course.classes_conducted}</td>
              <td>
                <StatusBadge {...STANDING[course.standing]} />
              </td>
            </tr>
          ))}
        </DataTable>
      </div>
      <Sheet open={open !== null} onClose={() => setOpen(null)} title={open?.course_title ?? "Course attendance"} description={open ? `${open.course_code}${open.instructor ? ` · ${open.instructor}` : ""}` : undefined}>
        <div className="px-5 py-5">{open && <CourseAttendanceDetail course={open} />}</div>
      </Sheet>
    </>
  );
}
