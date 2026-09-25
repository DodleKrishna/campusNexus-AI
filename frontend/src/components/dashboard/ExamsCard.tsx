import { StatusBadge } from "@/components/ui/badge";
import { CellTitle, DataTable } from "@/components/ui/data-table";
import { SkeletonTable } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { ELIGIBILITY } from "@/components/dashboard/status";
import { useExams } from "@/hooks/useStudentData";
import { formatDate, formatTime, titleCase } from "@/utils/format";

/** Upcoming exams with attendance-based eligibility (decided server-side). */
export function ExamsTable() {
  const { data, isLoading, isError, error, refetch } = useExams();
  if (isLoading) return <SkeletonTable rows={3} />;
  if (isError) return <ErrorState message={`CampusNexus couldn't load your exams. ${(error as Error).message}`} onRetry={() => void refetch()} />;
  if (!data || data.length === 0) return <EmptyState title="No upcoming exams" description="Scheduled exams appear here with your eligibility." />;
  return (
    <div className="overflow-hidden rounded-card border border-border bg-surface">
      <DataTable columns={["Course", "Date", "Time", "Venue", "Eligibility"]} caption="Upcoming exams">
        {data.map((exam) => {
          const eligibility = exam.eligibility ? ELIGIBILITY[exam.eligibility] : null;
          return (
            <tr key={`${exam.course_code}-${exam.starts_at}`}>
              <td>
                <CellTitle title={exam.course_title} subtitle={`${exam.course_code} · ${titleCase(exam.exam_type)}`} />
              </td>
              <td className="whitespace-nowrap">{formatDate(exam.starts_at)}</td>
              <td className="whitespace-nowrap tabular-nums">
                {formatTime(exam.starts_at)}–{formatTime(exam.ends_at)}
              </td>
              <td>{exam.venue}</td>
              <td>{eligibility ? <StatusBadge {...eligibility} title={exam.eligibility_caveat ?? undefined} /> : <span className="text-muted">—</span>}</td>
            </tr>
          );
        })}
      </DataTable>
    </div>
  );
}
