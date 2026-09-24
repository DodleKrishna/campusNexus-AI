import { AgentsGrid } from "@/components/dashboard/AgentsGrid";
import { AttendanceCard } from "@/components/dashboard/AttendanceCard";
import { ExamsCard } from "@/components/dashboard/ExamsCard";
import { NotificationsCard } from "@/components/dashboard/NotificationsCard";
import { RequestsCard } from "@/components/dashboard/RequestsCard";
import { SummaryCards, SummaryCardsSkeleton } from "@/components/dashboard/SummaryCards";
import { TodayScheduleCard } from "@/components/dashboard/TodayScheduleCard";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/states";
import { useDashboard } from "@/hooks/useStudentData";
import { formatLongDate, greeting } from "@/utils/format";

export function StudentDashboardPage() {
  const { data, isLoading, isError, error, refetch } = useDashboard();
  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        {data ? (
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">
              {greeting()}, {data.profile.first_name}
            </h1>
            <p className="mt-1 text-sm text-muted">
              {data.profile.department_name} · Year {data.profile.year} • Semester {data.profile.semester}
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

      {isLoading && <SummaryCardsSkeleton />}
      {isError && <ErrorState message={`CampusNexus couldn't load your dashboard. ${(error as Error).message}`} onRetry={() => void refetch()} />}
      {data && <SummaryCards data={data} />}

      <div className="grid gap-6 xl:grid-cols-3">
        <div className="space-y-6 xl:col-span-2">
          <TodayScheduleCard />
          <AttendanceCard />
          <NotificationsCard />
        </div>
        <div className="space-y-6">
          <ExamsCard limit={4} />
          <RequestsCard limit={4} />
        </div>
      </div>

      <section className="space-y-3">
        <div>
          <h2 className="text-base font-semibold">Your AI agents</h2>
          <p className="text-sm text-muted">Specialized agents that read your records, apply campus rules and explain the result.</p>
        </div>
        <AgentsGrid />
      </section>
    </div>
  );
}
