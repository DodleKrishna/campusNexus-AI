import { BarChart3, CalendarClock, ShieldAlert } from "lucide-react";
import { useState } from "react";
import { AskAgentLink } from "@/components/agents/AskAgentLink";
import { AttendanceTable } from "@/components/dashboard/AttendanceCard";
import { ExamsTable } from "@/components/dashboard/ExamsCard";
import { TodayScheduleCard } from "@/components/dashboard/TodayScheduleCard";
import { MetricCard, MetricSkeletons } from "@/components/ui/metric-card";
import { TabPanel, Tabs } from "@/components/ui/tabs";
import { useDashboard } from "@/hooks/useStudentData";
import { PageTitle } from "@/pages/PageTitle";
import { formatDate } from "@/utils/format";

function AcademicSummary({ onOpen }: { onOpen: (tab: string) => void }) {
  const { data, isLoading } = useDashboard();
  if (isLoading || !data) return <MetricSkeletons count={3} className="lg:grid-cols-3" />;
  const overall = data.overall_attendance;
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
      <button type="button" onClick={() => onOpen("attendance")} className="text-left">
        <MetricCard label="Overall attendance" icon={<BarChart3 />} value={overall ? `${overall.percentage.toFixed(1)}%` : "—"} context={overall ? `${overall.classes_attended}/${overall.classes_conducted} classes` : "No classes yet"} />
      </button>
      <button type="button" onClick={() => onOpen("attendance")} className="text-left">
        <MetricCard
          label="Courses below requirement"
          icon={<ShieldAlert />}
          value={data.courses_below_requirement}
          context={data.courses_below_requirement ? "Open attendance for recovery" : "All courses on track"}
          tone={data.courses_below_requirement ? "danger" : "default"}
        />
      </button>
      <button type="button" onClick={() => onOpen("exams")} className="text-left">
        <MetricCard label="Next exam" icon={<CalendarClock />} value={data.next_exam?.course_code ?? "None"} context={data.next_exam ? `${data.next_exam.course_title} · ${formatDate(data.next_exam.starts_at)}` : "No upcoming exams"} />
      </button>
    </div>
  );
}

export function StudentAcademicsPage() {
  const [tab, setTab] = useState("overview");
  return (
    <div className="space-y-6">
      <PageTitle
        title="My Academics"
        description="Attendance, exams and today's timetable from your records."
        action={<AskAgentLink to="/student/agents/academic" label="Ask Academic Agent" />}
      />
      <div>
        <Tabs
          label="Academics"
          value={tab}
          onChange={setTab}
          tabs={[
            { id: "overview", label: "Overview" },
            { id: "attendance", label: "Attendance" },
            { id: "exams", label: "Exams" },
            { id: "timetable", label: "Timetable" },
          ]}
        />
        <TabPanel>
          {tab === "overview" && <AcademicSummary onOpen={setTab} />}
          {tab === "attendance" && <AttendanceTable />}
          {tab === "exams" && <ExamsTable />}
          {tab === "timetable" && <TodayScheduleCard />}
        </TabPanel>
      </div>
    </div>
  );
}
