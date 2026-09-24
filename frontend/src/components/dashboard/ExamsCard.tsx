import { NotebookPen } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { ELIGIBILITY } from "@/components/dashboard/status";
import { useExams } from "@/hooks/useStudentData";
import { formatDate, formatTime, titleCase } from "@/utils/format";

export function ExamsCard({ limit }: { limit?: number }) {
  const { data, isLoading, isError, error, refetch } = useExams();
  const exams = limit ? data?.slice(0, limit) : data;
  return (
    <Card>
      <CardHeader icon={<NotebookPen />} title="Upcoming exams" description="Eligibility is attendance-based" />
      <CardBody>
        {isLoading && <SkeletonRows rows={3} />}
        {isError && <ErrorState message={`CampusNexus couldn't load your exams. ${(error as Error).message}`} onRetry={() => void refetch()} />}
        {exams && exams.length === 0 && <EmptyState title="No upcoming exams" />}
        {exams && exams.length > 0 && (
          <ul className="space-y-3">
            {exams.map((exam) => {
              const eligibility = exam.eligibility ? ELIGIBILITY[exam.eligibility] : null;
              return (
                <li key={`${exam.course_code}-${exam.starts_at}`} className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium">{exam.course_title}</div>
                    <div className="text-xs text-muted">
                      {titleCase(exam.exam_type)} · {formatDate(exam.starts_at)}, {formatTime(exam.starts_at)}–{formatTime(exam.ends_at)}
                    </div>
                    <div className="text-xs text-muted">{exam.venue}</div>
                  </div>
                  {eligibility && (
                    <Badge tone={eligibility.tone} title={exam.eligibility_caveat ?? undefined}>
                      {eligibility.label}
                    </Badge>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}
