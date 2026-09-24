import { AttendanceCard } from "@/components/dashboard/AttendanceCard";
import { ExamsCard } from "@/components/dashboard/ExamsCard";
import { TodayScheduleCard } from "@/components/dashboard/TodayScheduleCard";
import { PageTitle } from "@/pages/PageTitle";

export function StudentAcademicsPage() {
  return (
    <div className="space-y-6">
      <PageTitle title="My Academics" description="Attendance, today's classes and upcoming exams from your records." />
      <div className="grid gap-6 xl:grid-cols-3">
        <div className="space-y-6 xl:col-span-2">
          <AttendanceCard />
          <TodayScheduleCard />
        </div>
        <ExamsCard />
      </div>
    </div>
  );
}
