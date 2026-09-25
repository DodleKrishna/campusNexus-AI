import { AgentTiles } from "@/components/agents/AgentCards";
import { AttentionList, type AttentionItem } from "@/components/dashboard/AttentionList";
import { LiveClassCard } from "@/components/dashboard/LiveClassCard";
import { SummaryCards, SummaryCardsSkeleton } from "@/components/dashboard/SummaryCards";
import { TodayScheduleCard } from "@/components/dashboard/TodayScheduleCard";
import { Card, CardHeader } from "@/components/ui/card";
import { ErrorState } from "@/components/ui/states";
import { AGENTS } from "@/features/agents/catalog";
import { useAttendance, useDashboard, useExams, useRequests, useWorkflowRequests } from "@/hooks/useStudentData";
import { DashboardHeader } from "@/pages/PageTitle";
import { formatDate, formatLongDate, greeting } from "@/utils/format";

/** Only high-value items whose status the backend already decided. */
function useStudentAttention(): { items: AttentionItem[]; isLoading: boolean } {
  const attendance = useAttendance();
  const exams = useExams();
  const workflow = useWorkflowRequests();
  const approvals = useRequests();
  const items: AttentionItem[] = [];
  for (const c of attendance.data ?? []) {
    if (c.standing === "below_requirement" || c.standing === "at_risk") {
      items.push({
        id: `att-${c.course_code}`,
        tone: c.standing === "below_requirement" ? "danger" : "warning",
        kind: "Attendance",
        title: c.course_title,
        detail: `${c.current_percentage?.toFixed(1)}% attendance · ${c.standing === "below_requirement" ? "below" : "close to"} ${c.required_percentage}%`,
        to: "/student/academics",
      });
    }
  }
  for (const e of exams.data ?? []) {
    if (e.eligibility === "not_eligible") {
      items.push({ id: `exam-${e.course_code}-${e.starts_at}`, tone: "danger", kind: "Exam eligibility", title: `${e.course_title} · not eligible`, detail: formatDate(e.starts_at), to: "/student/academics" });
    }
  }
  for (const r of workflow.data ?? []) {
    if (r.status === "pending" || r.status === "needs_review") {
      items.push({
        id: `req-${r.request_id}`,
        tone: r.status === "needs_review" ? "caution" : "warning",
        kind: r.type_label,
        title: r.title,
        detail: r.status === "needs_review" ? "Waiting for a reviewer" : `Waiting for ${r.reviewer_name ?? "a decision"}`,
        to: "/student/requests",
      });
    }
  }
  for (const a of approvals.data ?? []) {
    if (a.status === "pending") items.push({ id: `appr-${a.approval_id}`, tone: "warning", kind: "Action approval", title: a.title, detail: "Awaiting approval", to: "/student/requests" });
  }
  return { items, isLoading: attendance.isLoading || exams.isLoading };
}

export function StudentDashboardPage() {
  const { data, isLoading, isError, error, refetch } = useDashboard();
  const attention = useStudentAttention();
  const profile = data?.profile;
  return (
    <div className="space-y-6">
      <DashboardHeader
        title={profile ? `${greeting()}, ${profile.first_name}` : undefined}
        context={profile ? `${profile.department_code} · Year ${profile.year} · Semester ${profile.semester}` : undefined}
        date={formatLongDate()}
      />

      {isLoading && <SummaryCardsSkeleton />}
      {isError && <ErrorState message={`CampusNexus couldn't load your dashboard. ${(error as Error).message}`} onRetry={() => void refetch()} />}
      {data && <SummaryCards data={data} />}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,65fr)_minmax(0,35fr)]">
        <TodayScheduleCard />
        <LiveClassCard />
      </div>

      <div className="grid grid-cols-1 items-start gap-6 xl:grid-cols-[minmax(0,65fr)_minmax(0,35fr)]">
        <AttentionList items={attention.items} isLoading={attention.isLoading} limit={5} />
        <Card>
          <CardHeader title="Quick access" />
          <div className="px-5 pb-5">
            <AgentTiles agents={AGENTS} basePath="/student/agents" className="sm:grid-cols-2" />
          </div>
        </Card>
      </div>
    </div>
  );
}
