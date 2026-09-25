import { AlertTriangle, CalendarDays, ClipboardList, GraduationCap, Radio, Users } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { ActivityList } from "@/components/hod/ActivityList";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { useHodDashboard } from "@/hooks/useHodData";
import { useWorkflowRequests } from "@/hooks/useStudentData";
import { formatLongDate } from "@/utils/format";

export function HodStat({ label, value, detail, icon, to, danger }: { label: string; value: ReactNode; detail: ReactNode; icon: ReactNode; to?: string; danger?: boolean }) {
  const body = (
    <Card className="h-full px-5 py-4 transition-colors hover:border-accent/40">
      <div className="flex items-center justify-between text-xs font-medium text-muted">
        {label}
        <span className="text-subtle [&_svg]:size-4">{icon}</span>
      </div>
      <div className={danger ? "mt-2 text-2xl font-semibold tracking-tight text-danger-strong" : "mt-2 text-2xl font-semibold tracking-tight"}>{value}</div>
      <div className="mt-1 line-clamp-2 text-xs text-muted">{detail}</div>
    </Card>
  );
  return to ? <Link to={to}>{body}</Link> : body;
}

function FacultyRequestsPreview() {
  const { data, isLoading } = useWorkflowRequests();
  const pending = data?.filter((r) => r.status === "pending") ?? [];
  return (
    <Card>
      <CardHeader
        icon={<ClipboardList />}
        title="Requests waiting for you"
        description="Faculty requests and escalated student requests"
        action={<Link to="/hod/requests" className="text-xs font-medium text-accent-hover">Open inbox</Link>}
      />
      <CardBody className="space-y-3">
        {isLoading && <Skeleton className="h-20 w-full" />}
        {data && pending.length === 0 && <EmptyState title="Nothing waiting" description="Requests routed to you appear here." />}
        {pending.slice(0, 3).map((r) => (
          <Link key={r.request_id} to="/hod/requests" className="block rounded-lg border border-border px-4 py-3 hover:border-accent/40">
            <p className="text-sm font-medium">
              {r.requester_name} — {r.type_label}
            </p>
            <p className="mt-0.5 text-xs text-muted">
              {r.title} · {r.context.affected_classes.length} affected class{r.context.affected_classes.length === 1 ? "" : "es"}
            </p>
          </Link>
        ))}
      </CardBody>
    </Card>
  );
}

export function HodDashboardPage() {
  const { data, isLoading, isError, error, refetch } = useHodDashboard();
  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        {data ? (
          <div>
            <p className="text-xs font-semibold uppercase tracking-wide text-accent-hover">{data.profile.department_name}</p>
            <h1 className="mt-1 text-2xl font-semibold tracking-tight">{data.profile.full_name}</h1>
            <p className="mt-1 text-sm text-muted">
              {data.profile.designation} · {data.profile.department_code}
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            <Skeleton className="h-4 w-56" />
            <Skeleton className="h-7 w-64" />
          </div>
        )}
        <p className="text-sm text-muted">
          {formatLongDate()}
          {data && ` · ${data.now_local} IST`}
        </p>
      </header>

      {isError && <ErrorState message={`CampusNexus couldn't load the department dashboard. ${(error as Error).message}`} onRetry={() => void refetch()} />}
      {data && (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-3 xl:grid-cols-6">
          <HodStat label="Faculty" icon={<Users />} value={data.faculty_count} detail={data.profile.department_code} to="/hod/faculty" />
          <HodStat label="Students" icon={<GraduationCap />} value={data.student_count} detail="Enrolled in the department" to="/hod/students" />
          <HodStat label="Classes today" icon={<CalendarDays />} value={data.classes_today} detail={`${data.completed_classes} completed · ${data.not_started_classes} not started`} />
          <HodStat label="Active classes" icon={<Radio />} value={data.active_classes} detail={data.active_classes ? "In session now" : "None in session"} />
          <HodStat label="Pending faculty requests" icon={<ClipboardList />} value={data.pending_faculty_requests} detail="Awaiting your decision" to="/hod/requests" />
          <HodStat
            label="Escalated student requests"
            icon={<AlertTriangle />}
            value={data.escalated_student_requests}
            detail="No faculty reviewer found"
            to="/hod/requests"
            danger={data.escalated_student_requests > 0}
          />
        </div>
      )}

      <div className="grid gap-6 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <ActivityList classes={data?.activity} graceMinutes={data?.start_grace_minutes} isLoading={isLoading} />
        </div>
        <FacultyRequestsPreview />
      </div>
    </div>
  );
}
