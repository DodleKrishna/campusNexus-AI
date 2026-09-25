import { CalendarDays, ClipboardList, Clock, Inbox, MapPin, Radio, School, Users } from "lucide-react";
import { Link } from "react-router-dom";
import { AgentTiles } from "@/components/agents/AgentCards";
import { classPath } from "@/components/faculty/paths";
import { TodayClassesCard } from "@/components/faculty/TodayClassesCard";
import { StatusBadge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button-variants";
import { Card, CardHeader } from "@/components/ui/card";
import { MetricCard, MetricGrid, MetricSkeletons } from "@/components/ui/metric-card";
import { ProgressBar } from "@/components/ui/progress";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { WORKFLOW_STATUS, statusOf } from "@/components/dashboard/status";
import { FACULTY_AGENTS } from "@/features/agents/facultyCatalog";
import { useFacultyDashboard } from "@/hooks/useFacultyData";
import { useWorkflowRequests } from "@/hooks/useStudentData";
import { DashboardHeader } from "@/pages/PageTitle";
import type { FacultyClass } from "@/types/api";
import { formatLongDate, formatTime, greeting, relativeTime } from "@/utils/format";

/** The class in session right now; otherwise when the next one starts. */
function ActiveClassPanel({ active, today }: { active: FacultyClass | null; today: FacultyClass[] }) {
  if (!active) {
    const next = today.find((c) => c.status === "scheduled");
    return (
      <Card className="h-full">
        <CardHeader title="Current class" />
        <EmptyState compact icon={<Radio />} title="No class in session" description={next ? `Your next class begins at ${next.start_local}.` : "You have no more classes to start today."} />
      </Card>
    );
  }
  const { tally } = active;
  const marked = tally.roster - tally.unmarked;
  return (
    <Card className="h-full border-primary/40">
      <CardHeader title="Current class" action={<StatusBadge label="Live" tone="live" pulse />} />
      <div className="px-5 pb-5">
        <p className="text-base font-semibold text-ink">{active.course_title}</p>
        <ul className="mt-2 space-y-1 text-[13px] text-muted [&_svg]:size-3.5 [&_svg]:text-subtle">
          <li className="flex items-center gap-2">
            <Users /> {active.department_code} {active.year}-{active.section}
          </li>
          <li className="flex items-center gap-2">
            <Clock /> {active.start_local}–{active.end_local}
            {active.actual_started_at && ` · started ${formatTime(active.actual_started_at)}`}
          </li>
          <li className="flex items-center gap-2">
            <MapPin /> {active.room}
          </li>
        </ul>
        <div className="mt-4">
          <div className="mb-1.5 flex items-baseline justify-between text-[13px]">
            <span className="text-muted">Attendance</span>
            <span className="font-semibold tabular-nums text-ink">
              {marked} / {tally.roster} marked
            </span>
          </div>
          <ProgressBar value={marked} max={tally.roster || 1} tone="accent" label="Students marked" />
        </div>
        <Link to={classPath(active)} className={buttonVariants({ className: "mt-4 w-full" })}>
          <School /> Open Class
        </Link>
      </div>
    </Card>
  );
}

function RecentRequests() {
  const { data, isLoading } = useWorkflowRequests();
  const pending = data?.filter((r) => r.status === "pending").slice(0, 4) ?? [];
  return (
    <Card>
      <CardHeader
        title="Requests requiring attention"
        action={
          <Link to="/faculty/requests" className="text-[13px] font-medium text-primary hover:underline">
            Open inbox
          </Link>
        }
      />
      {isLoading && <SkeletonRows rows={2} className="px-5 pb-5" />}
      {data && pending.length === 0 && <EmptyState compact icon={<Inbox />} title="No pending requests" description="You're all caught up." />}
      {pending.length > 0 && (
        <ul className="divide-y divide-border border-t border-border">
          {pending.map((r) => (
            <li key={r.request_id}>
              <Link to="/faculty/requests" className="flex items-center gap-3 px-5 py-3 transition-colors hover:bg-surface-muted/60">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-ink">
                    {r.student_name ?? r.requester_name} — {r.title}
                  </p>
                  <p className="text-xs text-muted">{relativeTime(r.submitted_at ?? r.created_at)}</p>
                </div>
                <StatusBadge {...statusOf(WORKFLOW_STATUS, r.status)} />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

export function FacultyDashboardPage() {
  const { data, isLoading, isError, error, refetch } = useFacultyDashboard();
  const active = data?.active_class ?? null;
  return (
    <div className="space-y-6">
      <DashboardHeader
        title={data ? `${greeting()}, ${data.profile.full_name}` : undefined}
        context={data ? data.profile.department_name : undefined}
        date={formatLongDate()}
      />

      {isError && <ErrorState message={`CampusNexus couldn't load your dashboard. ${(error as Error).message}`} onRetry={() => void refetch()} />}
      {isLoading && <MetricSkeletons />}
      {data && (
        <MetricGrid>
          <MetricCard
            label="Classes today"
            icon={<CalendarDays />}
            value={data.classes_today}
            context={data.classes_today ? `${data.today.filter((c) => c.status === "closed").length} completed` : "Nothing scheduled"}
            to="/faculty/classes"
          />
          <MetricCard
            label="Current class"
            icon={<Radio />}
            value={active ? active.course_code : "None"}
            context={active ? active.course_title : "No class in session"}
            to={active ? classPath(active) : undefined}
          />
          <MetricCard label="Students today" icon={<Users />} value={data.students_across_today} context="Across today's rosters" />
          <MetricCard
            label="Pending requests"
            icon={<ClipboardList />}
            value={data.pending_requests}
            context={data.pending_requests ? "Awaiting your decision" : "Inbox clear"}
            tone={data.pending_requests ? "warning" : "default"}
            to="/faculty/requests"
          />
        </MetricGrid>
      )}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,65fr)_minmax(0,35fr)]">
        <TodayClassesCard classes={data?.today} isLoading={isLoading} />
        {data && <ActiveClassPanel active={active} today={data.today} />}
      </div>

      <div className="grid grid-cols-1 items-start gap-6 xl:grid-cols-[minmax(0,65fr)_minmax(0,35fr)]">
        <RecentRequests />
        <Card>
          <CardHeader title="Quick access" />
          <div className="px-5 pb-5">
            <AgentTiles agents={FACULTY_AGENTS} basePath="/faculty/agents" className="sm:grid-cols-2" />
          </div>
        </Card>
      </div>
    </div>
  );
}
