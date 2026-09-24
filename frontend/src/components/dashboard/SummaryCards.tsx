import { CalendarClock, ClipboardList, GraduationCap, Percent } from "lucide-react";
import type { ReactNode } from "react";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { DashboardSummary } from "@/types/api";
import { cn } from "@/utils/cn";
import { formatDateTime } from "@/utils/format";

function Stat({ label, value, detail, icon, emphasis }: { label: string; value: ReactNode; detail: ReactNode; icon: ReactNode; emphasis?: "danger" }) {
  return (
    <Card className="px-5 py-4">
      <div className="flex items-center justify-between text-xs font-medium text-muted">
        {label}
        <span className="text-subtle [&_svg]:size-4">{icon}</span>
      </div>
      <div className={cn("mt-2 text-2xl font-semibold tracking-tight", emphasis === "danger" && "text-danger-strong")}>{value}</div>
      <div className="mt-1 truncate text-xs text-muted">{detail}</div>
    </Card>
  );
}

export function SummaryCardsSkeleton() {
  return (
    <div className="grid grid-cols-2 gap-4 xl:grid-cols-4">
      {Array.from({ length: 4 }, (_, i) => (
        <Card key={i} className="space-y-3 px-5 py-4">
          <Skeleton className="h-3 w-24" />
          <Skeleton className="h-7 w-20" />
          <Skeleton className="h-3 w-32" />
        </Card>
      ))}
    </div>
  );
}

export function SummaryCards({ data }: { data: DashboardSummary }) {
  const overall = data.overall_attendance;
  const next = data.next_exam;
  return (
    <div className="grid grid-cols-2 gap-4 xl:grid-cols-4">
      <Stat label="CGPA" icon={<GraduationCap />} value={data.cgpa.toFixed(2)} detail={`Semester ${data.profile.semester}`} />
      <Stat
        label="Overall attendance"
        icon={<Percent />}
        value={overall ? `${overall.percentage.toFixed(1)}%` : "—"}
        detail={
          overall
            ? data.courses_below_requirement > 0
              ? `${data.courses_below_requirement} course${data.courses_below_requirement > 1 ? "s" : ""} below requirement`
              : `${overall.classes_attended}/${overall.classes_conducted} classes`
            : "No classes recorded yet"
        }
        emphasis={data.courses_below_requirement > 0 ? "danger" : undefined}
      />
      <Stat
        label="Next exam"
        icon={<CalendarClock />}
        value={next ? next.course_code : "None"}
        detail={next ? `${next.course_title} · ${formatDateTime(next.starts_at)}` : "No upcoming exams"}
      />
      <Stat
        label="Pending requests"
        icon={<ClipboardList />}
        value={data.pending_requests}
        detail={data.pending_requests ? "Awaiting approval" : "Nothing waiting on approval"}
      />
    </div>
  );
}
