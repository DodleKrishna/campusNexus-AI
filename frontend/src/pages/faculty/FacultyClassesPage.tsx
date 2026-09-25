import { BookOpen } from "lucide-react";
import { TodayClassesCard } from "@/components/faculty/TodayClassesCard";
import { Card, CardHeader } from "@/components/ui/card";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { useFacultyProfile, useFacultyToday } from "@/hooks/useFacultyData";
import { PageTitle } from "@/pages/PageTitle";

export function FacultyClassesPage() {
  const today = useFacultyToday();
  const profile = useFacultyProfile();
  return (
    <div className="space-y-6">
      <PageTitle title="My Classes" description="Start today's classes, take attendance and close them when you finish." />
      <TodayClassesCard title="Today's classes" classes={today.data} isLoading={today.isLoading} error={today.error as Error | null} onRetry={() => void today.refetch()} />
      <Card>
        <CardHeader title="Teaching assignments" description="The courses and sections you are assigned this term" />
        {profile.isLoading && <SkeletonRows rows={2} className="px-5 pb-5" />}
        {profile.isError && (
          <div className="px-5 pb-5">
            <ErrorState message={(profile.error as Error).message} onRetry={() => void profile.refetch()} />
          </div>
        )}
        {profile.data && profile.data.assignments.length === 0 && <EmptyState compact icon={<BookOpen />} title="No teaching assignments" />}
        {profile.data && profile.data.assignments.length > 0 && (
          <ul className="divide-y divide-border border-t border-border">
            {profile.data.assignments.map((a) => (
              <li key={a.assignment_id} className="flex flex-col gap-2 px-5 py-3.5 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-ink">
                    {a.course_title} <span className="font-normal text-muted">({a.course_code})</span>
                  </p>
                  <p className="text-xs text-muted">
                    {a.department_code} Year {a.year}, Semester {a.semester}, Section {a.section} · {a.academic_term} · {a.roster_size} students
                  </p>
                </div>
                <ul className="shrink-0 space-y-0.5 text-xs text-muted sm:text-right">
                  {a.weekly_slots.map((slot) => (
                    <li key={`${slot.weekday}-${slot.start_time}`} className="tabular-nums">
                      {slot.weekday} {slot.start_time}–{slot.end_time} · {slot.room}
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
