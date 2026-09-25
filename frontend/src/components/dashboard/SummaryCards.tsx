import { CalendarClock, ClipboardList, GraduationCap, Percent } from "lucide-react";
import { MetricCard, MetricGrid, MetricSkeletons } from "@/components/ui/metric-card";
import type { DashboardSummary } from "@/types/api";
import { formatDate } from "@/utils/format";

export function SummaryCardsSkeleton() {
  return <MetricSkeletons count={4} />;
}

/** The student's four headline numbers; every value comes from the dashboard API. */
export function SummaryCards({ data }: { data: DashboardSummary }) {
  const overall = data.overall_attendance;
  const next = data.next_exam;
  const below = data.courses_below_requirement;
  return (
    <MetricGrid>
      <MetricCard
        label="Overall attendance"
        icon={<Percent />}
        value={overall ? `${overall.percentage.toFixed(1)}%` : "—"}
        context={
          overall
            ? below > 0
              ? `${below} course${below > 1 ? "s" : ""} below requirement`
              : `${overall.classes_attended}/${overall.classes_conducted} classes`
            : "No classes recorded yet"
        }
        tone={below > 0 ? "danger" : "default"}
        to="/student/academics"
      />
      <MetricCard label="CGPA" icon={<GraduationCap />} value={data.cgpa.toFixed(2)} context={`Semester ${data.profile.semester}`} />
      <MetricCard
        label="Next exam"
        icon={<CalendarClock />}
        value={next ? next.course_code : "None"}
        context={next ? `${next.course_title} · ${formatDate(next.starts_at)}` : "No upcoming exams"}
        to="/student/academics"
      />
      <MetricCard
        label="Pending requests"
        icon={<ClipboardList />}
        value={data.pending_requests}
        context={data.pending_requests ? "Awaiting approval" : "Nothing waiting"}
        tone={data.pending_requests ? "warning" : "default"}
        to="/student/requests"
      />
    </MetricGrid>
  );
}
