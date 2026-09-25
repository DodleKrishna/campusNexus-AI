import { CalendarDays, ClipboardList, GraduationCap, Users } from "lucide-react";
import { AgentTiles } from "@/components/agents/AgentCards";
import { AskAgentLink } from "@/components/agents/AskAgentLink";
import { AttentionList, type AttentionItem } from "@/components/dashboard/AttentionList";
import { ActivityList, DepartmentPulse } from "@/components/hod/ActivityList";
import { SectionHeading } from "@/components/ui/card";
import { MetricCard, MetricGrid, MetricSkeletons } from "@/components/ui/metric-card";
import { ErrorState } from "@/components/ui/states";
import { HOD_AGENTS } from "@/features/agents/hodCatalog";
import { useHodComplaints, useHodDashboard, useHodDepartment } from "@/hooks/useHodData";
import { useWorkflowRequests } from "@/hooks/useStudentData";
import { DashboardHeader } from "@/pages/PageTitle";
import type { DepartmentDashboard } from "@/types/api";
import { formatLongDate, greeting } from "@/utils/format";

/** Actionable department items only: delayed classes, requests, attendance risk, SLA breaches. */
function useHodAttention(dashboard?: DepartmentDashboard): AttentionItem[] {
  const requests = useWorkflowRequests();
  const department = useHodDepartment();
  const complaints = useHodComplaints();
  const items: AttentionItem[] = [];
  for (const c of dashboard?.activity ?? []) {
    if (c.state === "delayed" || c.state === "not_held") {
      items.push({
        id: `class-${c.assignment_id}-${c.scheduled_start}`,
        tone: c.state === "not_held" ? "danger" : "caution",
        kind: c.state === "not_held" ? "Class not held" : "Delayed class",
        title: `${c.course_title} · ${c.class_label}`,
        detail: `${c.faculty_name} · ${c.start_local}–${c.end_local}`,
        to: "/hod/department",
      });
    }
  }
  for (const r of requests.data?.filter((x) => x.status === "pending") ?? []) {
    const faculty = r.requester_kind === "faculty";
    items.push({
      id: `req-${r.request_id}`,
      tone: faculty ? "warning" : "caution",
      kind: faculty ? "Faculty request" : "Escalated student request",
      title: `${r.requester_name ?? r.student_name} — ${r.type_label}`,
      detail: `${r.context.affected_classes.length} affected class${r.context.affected_classes.length === 1 ? "" : "es"}`,
      to: "/hod/requests",
    });
  }
  const risk = department.data?.at_risk_students ?? 0;
  if (risk > 0) {
    items.push({
      id: "risk",
      tone: "danger",
      kind: "Attendance risk",
      title: `${risk} student${risk === 1 ? "" : "s"} below the requirement`,
      detail: department.data?.required_percentage != null ? `Below ${department.data.required_percentage}% in at least one course` : undefined,
      to: "/hod/attendance",
    });
  }
  const breached = complaints.data?.filter((c) => c.response_breached || c.resolution_breached).length ?? 0;
  if (breached > 0) items.push({ id: "sla", tone: "danger", kind: "Complaints", title: `${breached} complaint${breached === 1 ? "" : "s"} past SLA`, to: "/hod/complaints" });
  return items;
}

export function HodDashboardPage() {
  const { data, isLoading, isError, error, refetch } = useHodDashboard();
  const attention = useHodAttention(data);
  const pending = data ? data.pending_faculty_requests + data.escalated_student_requests : 0;
  return (
    <div className="space-y-6">
      <DashboardHeader
        title={data ? `${greeting()}, ${data.profile.full_name}` : undefined}
        context={data ? `Head of Department · ${data.profile.department_name}` : undefined}
        date={
          <>
            {formatLongDate()}
            {data && ` · ${data.now_local} IST`}
          </>
        }
      />

      {isError && <ErrorState message={`CampusNexus couldn't load the department dashboard. ${(error as Error).message}`} onRetry={() => void refetch()} />}
      {isLoading && <MetricSkeletons />}
      {data && (
        <MetricGrid>
          <MetricCard label="Faculty" icon={<Users />} value={data.faculty_count} context={data.profile.department_code} to="/hod/faculty" />
          <MetricCard label="Students" icon={<GraduationCap />} value={data.student_count} context="Enrolled" to="/hod/students" />
          <MetricCard label="Classes today" icon={<CalendarDays />} value={data.classes_today} context={`${data.completed_classes} completed`} />
          <MetricCard
            label="Pending requests"
            icon={<ClipboardList />}
            value={pending}
            context={`${data.pending_faculty_requests} faculty · ${data.escalated_student_requests} escalated`}
            tone={pending ? "warning" : "default"}
            to="/hod/requests"
          />
        </MetricGrid>
      )}

      <section className="space-y-3">
        <SectionHeading title="Department pulse" />
        <DepartmentPulse classes={data?.activity} />
      </section>

      <div className="grid grid-cols-1 items-start gap-6 xl:grid-cols-[minmax(0,65fr)_minmax(0,35fr)]">
        <ActivityList
          title="Today's department activity"
          classes={data?.activity}
          graceMinutes={data?.start_grace_minutes}
          isLoading={isLoading}
          action={<AskAgentLink to="/hod/agents/academic" label="Ask Academic Agent" />}
        />
        <div className="space-y-6">
          <AttentionList title="Needs attention" items={attention} isLoading={isLoading} limit={5} emptyDescription="No delayed classes, pending requests or SLA issues." />
          <section className="space-y-3">
            <SectionHeading title="Quick access" />
            <AgentTiles agents={HOD_AGENTS} basePath="/hod/agents" className="sm:grid-cols-2" />
          </section>
        </div>
      </div>
    </div>
  );
}
