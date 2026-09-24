import { AlertTriangle, ArrowRight, BookOpen, CalendarDays, ClipboardCheck, GraduationCap, Layers, MessageSquareWarning, ShieldAlert, Users } from "lucide-react";
import type { ReactNode } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { useAuth } from "@/auth/useAuth";
import { AgentWorkspace } from "@/components/agents/AgentWorkspace";
import { ActivityList } from "@/components/hod/ActivityList";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { CLASS_STATE } from "@/components/dashboard/status";
import { HOD_AGENTS, hodAgentByKey } from "@/features/agents/hodCatalog";
import { ReviewerInbox } from "@/features/requests/ReviewerInbox";
import { useHodAttendance, useHodComplaints, useHodDepartment, useHodFaculty, useHodStudents } from "@/hooks/useHodData";
import { HodStat } from "@/pages/hod/HodDashboardPage";
import { PageTitle } from "@/pages/PageTitle";
import type { FacultySummary } from "@/types/api";
import { formatDateTime } from "@/utils/format";

function Table({ columns, children }: { columns: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="border-b border-border text-xs text-muted">
            {columns.map((c) => (
              <th key={c} scope="col" className="py-2 pr-3 font-medium">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

const td = "py-2.5 pr-3 align-top";
const pct = (value: number | null) => (value == null ? "—" : `${value.toFixed(1)}%`);

function FacultyTable({ faculty }: { faculty: FacultySummary[] }) {
  return (
    <Table columns={["Faculty", "Teaches", "Classes today", "Now", "Requests"]}>
      {faculty.map((f) => (
        <tr key={f.faculty_id} className="border-b border-border last:border-0">
          <td className={td}>
            <div className="font-medium">
              {f.full_name} {f.is_hod && <Badge tone="primary">HOD</Badge>}
            </div>
            <div className="text-xs text-muted">
              {f.designation} · {f.employee_code}
            </div>
          </td>
          <td className={`${td} text-xs text-muted`}>{f.courses.join(", ") || "—"}</td>
          <td className={`${td} tabular-nums`}>
            {f.classes_today}
            {f.not_started_today > 0 && (
              <Badge tone="danger" className="ml-2">
                {f.not_started_today} not started
              </Badge>
            )}
          </td>
          <td className={td}>{f.active_class ? <Badge tone="accent">{f.active_class}</Badge> : <span className="text-xs text-muted">—</span>}</td>
          <td className={`${td} text-xs`}>
            {f.own_open_requests > 0 && <div>{f.own_open_requests} open request{f.own_open_requests === 1 ? "" : "s"} to HOD</div>}
            {f.pending_requests_to_review > 0 && <div className="text-muted">{f.pending_requests_to_review} student request{f.pending_requests_to_review === 1 ? "" : "s"} to review</div>}
            {!f.own_open_requests && !f.pending_requests_to_review && <span className="text-muted">—</span>}
          </td>
        </tr>
      ))}
    </Table>
  );
}

export function HodDepartmentPage() {
  const { data, isLoading, isError, error, refetch } = useHodDepartment();
  return (
    <div className="space-y-6">
      <PageTitle title={data ? data.dashboard.profile.department_name : "Department"} description="Faculty, students, today's classes, attendance risk and pending requests." />
      {isLoading && <SkeletonRows rows={5} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <>
          <div className="grid grid-cols-2 gap-4 xl:grid-cols-5">
            <HodStat label="Faculty" icon={<Users />} value={data.faculty.length} detail="In the department" to="/hod/faculty" />
            <HodStat label="Students" icon={<GraduationCap />} value={data.dashboard.student_count} detail="Enrolled" to="/hod/students" />
            <HodStat label="Classes today" icon={<CalendarDays />} value={data.dashboard.classes_today} detail={`${data.dashboard.not_started_classes} not started`} />
            <HodStat
              label="Attendance risk"
              icon={<ShieldAlert />}
              value={data.at_risk_students}
              detail={`Students below ${data.required_percentage ?? "—"}% in a course`}
              to="/hod/attendance"
              danger={data.at_risk_students > 0}
            />
            <HodStat label="Pending requests" icon={<AlertTriangle />} value={data.dashboard.pending_faculty_requests + data.dashboard.escalated_student_requests} detail="Faculty + escalated" to="/hod/requests" />
          </div>
          <ActivityList classes={data.dashboard.activity} graceMinutes={data.dashboard.start_grace_minutes} />
          <Card>
            <CardHeader icon={<Users />} title="Faculty" />
            <CardBody>
              <FacultyTable faculty={data.faculty} />
            </CardBody>
          </Card>
        </>
      )}
    </div>
  );
}

export function HodFacultyPage() {
  const { data, isLoading, isError, error, refetch } = useHodFaculty();
  return (
    <div className="space-y-6">
      <PageTitle title="Faculty" description="Department faculty, what they teach today and their requests." />
      {isLoading && <SkeletonRows rows={5} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <Card>
          <CardBody>{data.length ? <FacultyTable faculty={data} /> : <EmptyState title="No faculty in this department" />}</CardBody>
        </Card>
      )}
    </div>
  );
}

export function HodStudentsPage() {
  const { data, isLoading, isError, error, refetch } = useHodStudents();
  return (
    <div className="space-y-6">
      <PageTitle title="Students" description="Factual attendance and complaint indicators for every student in the department. No predictive scoring." />
      {isLoading && <SkeletonRows rows={6} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <Card>
          <CardHeader icon={<GraduationCap />} title={`${data.length} students`} description="Students with courses below the requirement are listed first" />
          <CardBody>
            <Table columns={["Student", "Year / Section", "Overall attendance", "Courses below requirement", "Open complaints"]}>
              {data.map((s) => (
                <tr key={s.student_id} className="border-b border-border last:border-0">
                  <td className={td}>
                    <div className="font-medium">{s.full_name}</div>
                    <div className="text-xs text-muted">{s.student_id}</div>
                  </td>
                  <td className={td}>
                    Year {s.year} · Sem {s.semester}
                    {s.section ? ` · Section ${s.section}` : ""}
                  </td>
                  <td className={`${td} tabular-nums`}>
                    {pct(s.overall_percentage)}
                    {s.classes_conducted > 0 && <span className="ml-1 text-xs text-muted">({s.classes_attended}/{s.classes_conducted})</span>}
                  </td>
                  <td className={td}>
                    {s.courses_below_threshold.length ? (
                      <div className="flex flex-wrap gap-1">
                        {s.courses_below_threshold.map((c) => (
                          <Badge key={c} tone="danger">
                            {c}
                          </Badge>
                        ))}
                      </div>
                    ) : (
                      <span className="text-xs text-muted">None</span>
                    )}
                  </td>
                  <td className={`${td} tabular-nums`}>{s.open_complaints || <span className="text-xs text-muted">0</span>}</td>
                </tr>
              ))}
            </Table>
          </CardBody>
        </Card>
      )}
    </div>
  );
}

export function HodAttendancePage() {
  const { data, isLoading, isError, error, refetch } = useHodAttendance();
  return (
    <div className="space-y-6">
      <PageTitle
        title="Attendance"
        description={data?.required_percentage != null ? `Against the ${data.required_percentage}% attendance policy requirement.` : "Department attendance from recorded sessions."}
      />
      {isLoading && <SkeletonRows rows={6} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <>
          <div className="grid gap-6 xl:grid-cols-2">
            <Card>
              <CardHeader icon={<Layers />} title="By section" />
              <CardBody>
                <Table columns={["Section", "Students", "Attended", "Attendance", "Below"]}>
                  {data.sections.map((s) => (
                    <tr key={s.class_label} className="border-b border-border last:border-0">
                      <td className={`${td} font-medium`}>{s.class_label}</td>
                      <td className={`${td} tabular-nums`}>{s.students}</td>
                      <td className={`${td} tabular-nums`}>{s.classes_attended}/{s.classes_conducted}</td>
                      <td className={`${td} tabular-nums`}>{pct(s.percentage)}</td>
                      <td className={td}>{s.below_threshold ? <Badge tone="danger">{s.below_threshold}</Badge> : "0"}</td>
                    </tr>
                  ))}
                </Table>
              </CardBody>
            </Card>
            <Card>
              <CardHeader icon={<BookOpen />} title="By course" />
              <CardBody>
                <Table columns={["Course", "Faculty", "Attendance", "Below"]}>
                  {data.courses.map((c) => (
                    <tr key={c.assignment_id} className="border-b border-border last:border-0">
                      <td className={td}>
                        <div className="font-medium">{c.course_title}</div>
                        <div className="text-xs text-muted">
                          {c.course_code} · {c.class_label} · {c.students} students
                        </div>
                      </td>
                      <td className={`${td} text-xs`}>{c.faculty_name}</td>
                      <td className={`${td} tabular-nums`}>{pct(c.percentage)}</td>
                      <td className={td}>{c.below_threshold ? <Badge tone="danger">{c.below_threshold}</Badge> : "0"}</td>
                    </tr>
                  ))}
                </Table>
              </CardBody>
            </Card>
          </div>
          <Card>
            <CardHeader icon={<ShieldAlert />} title="Students below the requirement" description="Per course, lowest first" />
            <CardBody>
              {data.low_attendance.length === 0 ? (
                <EmptyState title="Nobody is below the requirement" />
              ) : (
                <Table columns={["Student", "Section", "Course", "Attended", "Attendance"]}>
                  {data.low_attendance.map((e) => (
                    <tr key={`${e.student_id}-${e.course_code}`} className="border-b border-border last:border-0">
                      <td className={td}>
                        <div className="font-medium">{e.full_name}</div>
                        <div className="text-xs text-muted">{e.student_id}</div>
                      </td>
                      <td className={td}>{e.class_label}</td>
                      <td className={td}>{e.course_title}</td>
                      <td className={`${td} tabular-nums`}>{e.classes_attended}/{e.classes_conducted}</td>
                      <td className={td}>
                        <Badge tone="danger">{e.percentage.toFixed(1)}%</Badge>
                      </td>
                    </tr>
                  ))}
                </Table>
              )}
            </CardBody>
          </Card>
          <div className="grid gap-6 xl:grid-cols-2">
            <Card>
              <CardHeader icon={<ClipboardCheck />} title="Recent sessions" />
              <CardBody>
                {data.recent_sessions.length === 0 ? (
                  <EmptyState title="No sessions recorded yet" />
                ) : (
                  <Table columns={["Class", "When", "Present", "Absent", "Status"]}>
                    {data.recent_sessions.map((s) => (
                      <tr key={s.session_id} className="border-b border-border last:border-0">
                        <td className={td}>
                          <div className="font-medium">{s.course_title}</div>
                          <div className="text-xs text-muted">
                            {s.class_label} · {s.faculty_name}
                          </div>
                        </td>
                        <td className={`${td} text-xs`}>{s.actual_started_at ? formatDateTime(s.actual_started_at) : s.session_date}</td>
                        <td className={`${td} tabular-nums`}>{s.tally.present + s.tally.late}/{s.tally.roster}</td>
                        <td className={`${td} tabular-nums`}>{s.tally.absent}</td>
                        <td className={td}>
                          <Badge tone={s.status === "active" ? "accent" : "neutral"}>{s.status === "active" ? "Live" : "Closed"}</Badge>
                        </td>
                      </tr>
                    ))}
                  </Table>
                )}
              </CardBody>
            </Card>
            <Card>
              <CardHeader icon={<AlertTriangle />} title="Incomplete or missed" description="Live sessions with students not yet marked, and classes not held today" />
              <CardBody className="space-y-2">
                {data.incomplete_sessions.length === 0 && data.not_held_today.length === 0 && <EmptyState title="Nothing incomplete" />}
                {data.incomplete_sessions.map((s) => (
                  <div key={s.session_id} className="flex items-center justify-between rounded-lg border border-border px-3 py-2 text-sm">
                    <span>
                      {s.course_title} · {s.class_label} · {s.faculty_name}
                    </span>
                    <Badge tone="warning">{s.tally.unmarked} not marked</Badge>
                  </div>
                ))}
                {data.not_held_today.map((c) => (
                  <div key={`${c.assignment_id}-${c.scheduled_start}`} className="flex items-center justify-between rounded-lg border border-border px-3 py-2 text-sm">
                    <span>
                      {c.course_title} · {c.class_label} · {c.start_local}–{c.end_local} · {c.faculty_name}
                    </span>
                    <Badge tone={CLASS_STATE.not_held.tone}>{CLASS_STATE.not_held.label}</Badge>
                  </div>
                ))}
              </CardBody>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}

export function HodComplaintsPage() {
  const { data, isLoading, isError, error, refetch } = useHodComplaints();
  return (
    <div className="space-y-6">
      <PageTitle title="Complaints" description="Complaints filed by the department's students, with their SLA state. Read-only." />
      {isLoading && <SkeletonRows rows={4} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <Card>
          <CardHeader icon={<MessageSquareWarning />} title={`${data.length} complaints`} />
          <CardBody>
            {data.length === 0 ? (
              <EmptyState title="No complaints from this department's students" />
            ) : (
              <Table columns={["Case", "Student", "Category", "Office", "Status", "SLA"]}>
                {data.map((c) => (
                  <tr key={c.case_code} className="border-b border-border last:border-0">
                    <td className={`${td} font-medium`}>{c.case_code}</td>
                    <td className={td}>{c.student_name}</td>
                    <td className={td}>{c.category.replace(/_/g, " ")}</td>
                    <td className={`${td} text-xs`}>{c.office}</td>
                    <td className={td}>
                      <Badge tone="neutral">{c.status.replace(/_/g, " ")}</Badge>
                    </td>
                    <td className={td}>
                      {c.response_breached || c.resolution_breached ? (
                        <Badge tone="danger">{c.resolution_breached ? "Resolution breached" : "Response breached"}</Badge>
                      ) : (
                        <span className="text-xs text-muted">{c.resolution_due_at ? `Due ${formatDateTime(c.resolution_due_at)}` : "—"}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </Table>
            )}
          </CardBody>
        </Card>
      )}
    </div>
  );
}

export function HodRequestsPage() {
  return (
    <div className="max-w-4xl space-y-6">
      <PageTitle title="Requests" description="Faculty requests from your department, and student requests escalated because no faculty reviewer could be found." />
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
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {HOD_AGENTS.map((agent) => (
          <Link key={agent.key} to={`/hod/agents/${agent.key}`} className="block rounded-[var(--radius-card)]">
            <Card className="flex h-full items-start gap-4 px-5 py-4 transition-colors hover:border-accent/40">
              <div className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-accent-soft text-accent-hover">
                <agent.icon className="size-5" />
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-semibold">{agent.name}</span>
                  <Badge tone="info">Read-only</Badge>
                </div>
                <p className="mt-1 text-xs leading-relaxed text-muted">{agent.responsibility}</p>
                <span className="mt-2 inline-flex items-center gap-1 text-xs font-medium text-accent-hover">
                  Open <ArrowRight className="size-3.5" />
                </span>
              </div>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  );
}

export function HodAgentPage() {
  const { agentKey } = useParams();
  const { user } = useAuth();
  const agent = hodAgentByKey(agentKey);
  if (!agent) return <Navigate to="/hod/agents" replace />;
  if (!user) return null;
  return <AgentWorkspace key={agent.key} agent={agent} userId={user.id} scope="hod" />;
}
