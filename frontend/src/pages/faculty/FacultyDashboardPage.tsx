import { CalendarDays, ClipboardList, Radio, Users } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { classPath } from "@/components/faculty/paths";
import { TodayClassesCard } from "@/components/faculty/TodayClassesCard";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { useFacultyDashboard } from "@/hooks/useFacultyData";
import { useWorkflowRequests } from "@/hooks/useStudentData";
import { formatLongDate, greeting } from "@/utils/format";

function Stat({ label, value, detail, icon, to }: { label: string; value: ReactNode; detail: ReactNode; icon: ReactNode; to?: string }) {
  const body = (
    <Card className="h-full px-5 py-4 transition-colors hover:border-accent/40">
      <div className="flex items-center justify-between text-xs font-medium text-muted">
        {label}
        <span className="text-subtle [&_svg]:size-4">{icon}</span>
      </div>
      <div className="mt-2 truncate text-2xl font-semibold tracking-tight">{value}</div>
      <div className="mt-1 truncate text-xs text-muted">{detail}</div>
    </Card>
  );
  return to ? <Link to={to}>{body}</Link> : body;
}

function PendingRequests() {
  const { data, isLoading, isError, error, refetch } = useWorkflowRequests();
  const pending = data?.filter((r) => r.status === "pending") ?? [];
  return (
    <Card>
      <CardHeader
        icon={<ClipboardList />}
        title="Student requests"
        description="Waiting for your decision"
        action={<Link to="/faculty/requests" className="text-xs font-medium text-accent-hover">Open inbox</Link>}
      />
      <CardBody className="space-y-3">
        {isLoading && <Skeleton className="h-20 w-full" />}
        {isError && <ErrorState message={`CampusNexus couldn't load requests. ${(error as Error).message}`} onRetry={() => void refetch()} />}
        {data && pending.length === 0 && <EmptyState title="No pending requests" description="Requests routed to you appear here." />}
        {pending.slice(0, 2).map((request) => (
          <Link key={request.request_id} to="/faculty/requests" className="block">
            <div className="rounded-lg border border-border px-4 py-3 hover:border-accent/40">
              <p className="text-sm font-medium">{request.student_name} — {request.title}</p>
              <p className="mt-0.5 line-clamp-1 text-xs text-muted">{request.reason}</p>
            </div>
          </Link>
        ))}
      </CardBody>
    </Card>
  );
}

export function FacultyDashboardPage() {
  const { data, isLoading, isError, error, refetch } = useFacultyDashboard();
  const active = data?.active_class;
  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        {data ? (
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">
              {greeting()}, {data.profile.full_name}
            </h1>
            <p className="mt-1 text-sm text-muted">
              {data.profile.designation} · {data.profile.department_name} · {data.profile.employee_code}
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            <Skeleton className="h-7 w-64" />
            <Skeleton className="h-4 w-80" />
          </div>
        )}
        <p className="text-sm text-muted">{formatLongDate()}</p>
      </header>

      {isError && <ErrorState message={`CampusNexus couldn't load your dashboard. ${(error as Error).message}`} onRetry={() => void refetch()} />}
      {data && (
        <div className="grid grid-cols-2 gap-4 xl:grid-cols-4">
          <Stat label="Classes today" icon={<CalendarDays />} value={data.classes_today} detail={data.classes_today ? `${data.today.filter((c) => c.status === "closed").length} completed` : "Nothing scheduled"} to="/faculty/classes" />
          <Stat
            label="Active class"
            icon={<Radio />}
            value={active ? active.course_code : "None"}
            detail={active ? `${active.course_title} · ${active.tally.present + active.tally.late}/${active.tally.roster} present` : "No class in session"}
            to={active ? classPath(active) : undefined}
          />
          <Stat label="Students across today's classes" icon={<Users />} value={data.students_across_today} detail="Distinct students on today's rosters" />
          <Stat label="Pending student requests" icon={<ClipboardList />} value={data.pending_requests} detail={data.pending_requests ? "Awaiting your decision" : "Inbox clear"} to="/faculty/requests" />
        </div>
      )}

      <div className="grid gap-6 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <TodayClassesCard classes={data?.today} isLoading={isLoading} />
        </div>
        <PendingRequests />
      </div>
    </div>
  );
}
