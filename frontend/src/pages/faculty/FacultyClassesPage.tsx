import { BookOpen } from "lucide-react";
import { TodayClassesCard } from "@/components/faculty/TodayClassesCard";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
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
      <TodayClassesCard classes={today.data} isLoading={today.isLoading} error={today.error as Error | null} onRetry={() => void today.refetch()} />
      <Card>
        <CardHeader icon={<BookOpen />} title="Teaching assignments" description="The courses and sections you are assigned this term" />
        <CardBody>
          {profile.isLoading && <SkeletonRows rows={2} />}
          {profile.isError && <ErrorState message={(profile.error as Error).message} onRetry={() => void profile.refetch()} />}
          {profile.data && profile.data.assignments.length === 0 && <EmptyState title="No teaching assignments" />}
          {profile.data && profile.data.assignments.length > 0 && (
            <ul className="divide-y divide-border">
              {profile.data.assignments.map((a) => (
                <li key={a.assignment_id} className="flex flex-wrap items-center justify-between gap-3 py-3">
                  <div>
                    <p className="text-sm font-medium">
                      {a.course_title} <span className="text-muted">({a.course_code})</span>
                    </p>
                    <p className="text-xs text-muted">
                      {a.department_code} Year {a.year}, Semester {a.semester}, Section {a.section} · {a.academic_term} · {a.roster_size} students
                    </p>
                  </div>
                  <div className="text-right text-xs text-muted">
                    {a.weekly_slots.map((slot) => (
                      <div key={`${slot.weekday}-${slot.start_time}`}>
                        {slot.weekday} {slot.start_time}–{slot.end_time} · {slot.room}
                      </div>
                    ))}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
