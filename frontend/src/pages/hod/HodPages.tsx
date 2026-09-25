import { CalendarDays, ClipboardCheck, GraduationCap, MessageSquareWarning, ShieldAlert, Users } from "lucide-react";
import { useState } from "react";
import { AgentDirectory } from "@/components/agents/AgentCards";
import { RoleAgentPage } from "@/components/agents/RoleAgentPage";
import { AskAgentLink } from "@/components/agents/AskAgentLink";
import { ActivityList, DepartmentPulse } from "@/components/hod/ActivityList";
import { StatusBadge } from "@/components/ui/badge";
import { Card, CardHeader } from "@/components/ui/card";
import { CellTitle, DataTable } from "@/components/ui/data-table";
import { SearchInput, Toolbar } from "@/components/ui/input";
import { MetricCard } from "@/components/ui/metric-card";
import { SkeletonTable } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { TabPanel, Tabs } from "@/components/ui/tabs";
import { CLASS_STATE } from "@/components/dashboard/status";
import { HOD_AGENTS, hodAgentByKey } from "@/features/agents/hodCatalog";
import { ReviewerInbox } from "@/features/requests/ReviewerInbox";
import { ComplaintsTable, FacultyTable } from "@/components/hod/DepartmentTables";
import { useHodAttendance, useHodComplaints, useHodDepartment, useHodFaculty, useHodStudents } from "@/hooks/useHodData";
import { PageTitle } from "@/pages/PageTitle";
import { formatDateTime } from "@/utils/format";

const pct = (value: number | null) => (value == null ? "—" : `${value.toFixed(1)}%`);
const matches = (q: string, ...fields: (string | null | undefined)[]) => !q || fields.some((f) => f?.toLowerCase().includes(q));

export function HodDepartmentPage() {
  const { data, isLoading, isError, error, refetch } = useHodDepartment();
  return (
    <div className="space-y-6">
      <PageTitle title={data ? data.dashboard.profile.department_name : "Department"} description="Today's classes, faculty, attendance risk and pending requests." />
      {isLoading && <SkeletonTable rows={5} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <>
          <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
            <MetricCard label="Faculty" icon={<Users />} value={data.faculty.length} context="In the department" to="/hod/faculty" />
            <MetricCard label="Students" icon={<GraduationCap />} value={data.dashboard.student_count} context="Enrolled" to="/hod/students" />
            <MetricCard label="Classes today" icon={<CalendarDays />} value={data.dashboard.classes_today} context={`${data.dashboard.not_started_classes} not started`} />
            <MetricCard
              label="Attendance risk"
              icon={<ShieldAlert />}
              value={data.at_risk_students}
              context={`Below ${data.required_percentage ?? "—"}% in a course`}
              to="/hod/attendance"
              tone={data.at_risk_students > 0 ? "danger" : "default"}
            />
          </div>
          <DepartmentPulse classes={data.dashboard.activity} />
          <ActivityList classes={data.dashboard.activity} graceMinutes={data.dashboard.start_grace_minutes} />
          <Card className="overflow-hidden">
            <CardHeader title="Faculty" description={`${data.faculty.length} members`} />
            <div className="border-t border-border">
              <FacultyTable faculty={data.faculty} />
            </div>
          </Card>
        </>
      )}
    </div>
  );
}

export function HodFacultyPage() {
  const { data, isLoading, isError, error, refetch } = useHodFaculty();
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const rows = data?.filter((f) => matches(q, f.full_name, f.designation, f.employee_code, ...f.courses)) ?? [];
  return (
    <div className="space-y-6">
      <PageTitle title="Faculty" description="Department faculty, what they teach today and their requests." />
      <Toolbar>
        <SearchInput value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search faculty or courses" aria-label="Search faculty" />
      </Toolbar>
      {isLoading && <SkeletonTable rows={5} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <Card className="overflow-hidden">
          {rows.length ? <FacultyTable faculty={rows} /> : <EmptyState icon={<Users />} title={data.length ? "No faculty match" : "No faculty in this department"} />}
        </Card>
      )}
    </div>
  );
}

export function HodStudentsPage() {
  const { data, isLoading, isError, error, refetch } = useHodStudents();
  const [query, setQuery] = useState("");
  const [riskOnly, setRiskOnly] = useState(false);
  const q = query.trim().toLowerCase();
  const rows = data?.filter((s) => matches(q, s.full_name, s.student_id) && (!riskOnly || s.courses_below_threshold.length > 0)) ?? [];
  return (
    <div className="space-y-6">
      <PageTitle title="Students" description="Factual attendance and complaint indicators for every student in the department. No predictive scoring." />
      <Toolbar>
        <SearchInput value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search by name or ID" aria-label="Search students" />
        <label className="inline-flex items-center gap-2 text-sm text-ink">
          <input type="checkbox" checked={riskOnly} onChange={(e) => setRiskOnly(e.target.checked)} className="size-4 accent-accent" />
          Below requirement only
        </label>
        {data && <span className="text-sm text-muted sm:ml-auto">{rows.length} of {data.length} students</span>}
      </Toolbar>
      {isLoading && <SkeletonTable rows={6} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <Card className="overflow-hidden">
          {rows.length === 0 ? (
            <EmptyState icon={<GraduationCap />} title="No students match" description="Try a different search or filter." />
          ) : (
            <DataTable columns={["Student", "Year / Section", "Attendance", "Below requirement", "Open complaints"]} caption="Department students">
              {rows.map((s) => (
                <tr key={s.student_id}>
                  <td>
                    <CellTitle title={s.full_name} subtitle={s.student_id} />
                  </td>
                  <td>
                    Year {s.year}
                    {s.section ? ` · ${s.section}` : ""}
                  </td>
                  <td className="tabular-nums">{pct(s.overall_percentage)}</td>
                  <td>
                    {s.courses_below_threshold.length ? (
                      <span className="inline-flex flex-wrap justify-end gap-1">
                        {s.courses_below_threshold.map((c) => (
                          <StatusBadge key={c} label={c} tone="danger" />
                        ))}
                      </span>
                    ) : (
                      <span className="text-muted">None</span>
                    )}
                  </td>
                  <td className="tabular-nums">{s.open_complaints || <span className="text-muted">0</span>}</td>
                </tr>
              ))}
            </DataTable>
          )}
        </Card>
      )}
    </div>
  );
}

export function HodAttendancePage() {
  const { data, isLoading, isError, error, refetch } = useHodAttendance();
  const [tab, setTab] = useState("sections");
  return (
    <div className="space-y-6">
      <PageTitle
        title="Attendance"
        description={data?.required_percentage != null ? `Against the ${data.required_percentage}% attendance policy requirement.` : "Department attendance from recorded sessions."}
        action={<AskAgentLink to="/hod/agents/academic" label="Ask Academic Agent" />}
      />
      {isLoading && <SkeletonTable rows={6} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <div>
          <Tabs
            label="Attendance views"
            value={tab}
            onChange={setTab}
            tabs={[
              { id: "sections", label: "By section", count: data.sections.length },
              { id: "courses", label: "By course", count: data.courses.length },
              { id: "below", label: "Below requirement", count: data.low_attendance.length },
              { id: "sessions", label: "Sessions", count: data.incomplete_sessions.length + data.not_held_today.length || undefined },
            ]}
          />
          {tab === "sections" && (
            <TabPanel>
              <Card className="overflow-hidden">
                <DataTable columns={["Section", "Students", "Attended", "Attendance", "Below"]} caption="Attendance by section">
                  {data.sections.map((s) => (
                    <tr key={s.class_label}>
                      <td className="font-medium">{s.class_label}</td>
                      <td className="tabular-nums">{s.students}</td>
                      <td className="tabular-nums">
                        {s.classes_attended}/{s.classes_conducted}
                      </td>
                      <td className="tabular-nums">{pct(s.percentage)}</td>
                      <td>{s.below_threshold ? <StatusBadge label={String(s.below_threshold)} tone="danger" /> : <span className="text-muted">0</span>}</td>
                    </tr>
                  ))}
                </DataTable>
              </Card>
            </TabPanel>
          )}
          {tab === "courses" && (
            <TabPanel>
              <Card className="overflow-hidden">
                <DataTable columns={["Course", "Faculty", "Attendance", "Below"]} caption="Attendance by course">
                  {data.courses.map((c) => (
                    <tr key={c.assignment_id}>
                      <td>
                        <CellTitle title={c.course_title} subtitle={`${c.course_code} · ${c.class_label} · ${c.students} students`} />
                      </td>
                      <td>{c.faculty_name}</td>
                      <td className="tabular-nums">{pct(c.percentage)}</td>
                      <td>{c.below_threshold ? <StatusBadge label={String(c.below_threshold)} tone="danger" /> : <span className="text-muted">0</span>}</td>
                    </tr>
                  ))}
                </DataTable>
              </Card>
            </TabPanel>
          )}
          {tab === "below" && (
            <TabPanel>
              <Card className="overflow-hidden">
                {data.low_attendance.length === 0 ? (
                  <EmptyState title="Nobody is below the requirement" description="Every student meets the attendance requirement in every course." />
                ) : (
                  <DataTable columns={["Student", "Section", "Course", "Attended", "Attendance"]} caption="Students below the requirement">
                    {data.low_attendance.map((e) => (
                      <tr key={`${e.student_id}-${e.course_code}`}>
                        <td>
                          <CellTitle title={e.full_name} subtitle={e.student_id} />
                        </td>
                        <td>{e.class_label}</td>
                        <td>{e.course_title}</td>
                        <td className="tabular-nums">
                          {e.classes_attended}/{e.classes_conducted}
                        </td>
                        <td>
                          <StatusBadge label={`${e.percentage.toFixed(1)}%`} tone="danger" />
                        </td>
                      </tr>
                    ))}
                  </DataTable>
                )}
              </Card>
            </TabPanel>
          )}
          {tab === "sessions" && (
            <TabPanel className="grid grid-cols-1 gap-6 lg:grid-cols-2">
              <Card className="overflow-hidden">
                <CardHeader title="Recent sessions" />
                {data.recent_sessions.length === 0 ? (
                  <EmptyState compact icon={<ClipboardCheck />} title="No sessions recorded yet" />
                ) : (
                  <div className="border-t border-border">
                    <DataTable columns={["Class", "When", "Present", "Status"]} caption="Recent sessions">
                      {data.recent_sessions.map((s) => (
                        <tr key={s.session_id}>
                          <td>
                            <CellTitle title={s.course_title} subtitle={`${s.class_label} · ${s.faculty_name}`} />
                          </td>
                          <td className="text-xs whitespace-nowrap">{s.actual_started_at ? formatDateTime(s.actual_started_at) : s.session_date}</td>
                          <td className="tabular-nums">
                            {s.tally.present + s.tally.late}/{s.tally.roster}
                          </td>
                          <td>{s.status === "active" ? <StatusBadge label="Live" tone="success" pulse /> : <StatusBadge label="Closed" tone="neutral" />}</td>
                        </tr>
                      ))}
                    </DataTable>
                  </div>
                )}
              </Card>
              <Card>
                <CardHeader title="Incomplete or missed" description="Live sessions with students not yet marked, and classes not held today" />
                {data.incomplete_sessions.length === 0 && data.not_held_today.length === 0 ? (
                  <EmptyState compact title="Nothing incomplete" description="Every session is fully marked." />
                ) : (
                  <ul className="divide-y divide-border border-t border-border">
                    {data.incomplete_sessions.map((s) => (
                      <li key={s.session_id} className="flex items-start justify-between gap-3 px-5 py-3">
                        <CellTitle title={s.course_title} subtitle={`${s.class_label} · ${s.faculty_name}`} />
                        <StatusBadge label={`${s.tally.unmarked} not marked`} tone="warning" />
                      </li>
                    ))}
                    {data.not_held_today.map((c) => (
                      <li key={`${c.assignment_id}-${c.scheduled_start}`} className="flex items-start justify-between gap-3 px-5 py-3">
                        <CellTitle title={c.course_title} subtitle={`${c.class_label} · ${c.start_local}–${c.end_local} · ${c.faculty_name}`} />
                        <StatusBadge {...CLASS_STATE.not_held} />
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
            </TabPanel>
          )}
        </div>
      )}
    </div>
  );
}

export function HodComplaintsPage() {
  const { data, isLoading, isError, error, refetch } = useHodComplaints();
  const [breachedOnly, setBreachedOnly] = useState(false);
  const rows = data?.filter((c) => !breachedOnly || c.response_breached || c.resolution_breached) ?? [];
  return (
    <div className="space-y-6">
      <PageTitle
        title="Complaints"
        description="Complaints filed by the department's students, with their SLA state. Read-only."
        action={<AskAgentLink to="/hod/agents/complaints" label="Ask Complaints Agent" />}
      />
      <Toolbar>
        <label className="inline-flex items-center gap-2 text-sm text-ink">
          <input type="checkbox" checked={breachedOnly} onChange={(e) => setBreachedOnly(e.target.checked)} className="size-4 accent-accent" />
          SLA breached only
        </label>
        {data && <span className="text-sm text-muted sm:ml-auto">{rows.length} complaints</span>}
      </Toolbar>
      {isLoading && <SkeletonTable rows={4} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <Card className="overflow-hidden">
          {rows.length === 0 ? (
            <EmptyState
              icon={<MessageSquareWarning />}
              title={data.length ? "No complaints past SLA" : "No open complaints."}
              description={data.length ? "Every complaint is within its service deadline." : "Complaints from this department's students appear here."}
            />
          ) : (
            <ComplaintsTable complaints={rows} />
          )}
        </Card>
      )}
    </div>
  );
}

export function HodRequestsPage() {
  return (
    <div className="space-y-6">
      <PageTitle
        title="Requests"
        description="Faculty requests from your department, and student requests escalated because no faculty reviewer could be found."
        action={<AskAgentLink to="/hod/agents/permission" label="Ask Permission Agent" />}
      />
      <ReviewerInbox
        groups={[
          { title: "Faculty requests", filter: (r) => r.requester_kind === "faculty" },
          { title: "Escalated student requests", filter: (r) => r.requester_kind !== "faculty" },
        ]}
      />
    </div>
  );
}

export function HodAgentsPage() {
  return (
    <div className="space-y-6">
      <PageTitle title="Agents" description="Department-scoped agents. They read your department's records only and never change anything." />
      <AgentDirectory agents={HOD_AGENTS} basePath="/hod/agents" />
    </div>
  );
}

export function HodAgentPage() {
  return <RoleAgentPage scope="hod" resolve={hodAgentByKey} basePath="/hod/agents" />;
}
