import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRight,
  Building2,
  CalendarDays,
  ClipboardList,
  Cpu,
  GraduationCap,
  KeyRound,
  Radio,
  ScrollText,
  ShieldAlert,
  Users,
} from "lucide-react";
import { useState } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { ApiError } from "@/api/client";
import { api, queryKeys } from "@/api/endpoints";
import { useAuth } from "@/auth/useAuth";
import { AgentWorkspace } from "@/components/agents/AgentWorkspace";
import { ActivityList } from "@/components/hod/ActivityList";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { DataTable } from "@/components/ui/data-table";
import { cell } from "@/components/ui/table-styles";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState, Notice } from "@/components/ui/states";
import { ADMIN_AGENTS, adminAgentByKey } from "@/features/agents/adminCatalog";
import { ReviewerInbox } from "@/features/requests/ReviewerInbox";
import {
  useAdminAIOperations,
  useAdminAttendance,
  useAdminAudit,
  useAdminComplaints,
  useAdminDashboard,
  useAdminDepartment,
  useAdminDepartments,
  useAdminSystem,
  useAdminUsers,
} from "@/hooks/useAdminData";
import { HodStat } from "@/pages/hod/HodDashboardPage";
import { PageTitle } from "@/pages/PageTitle";
import type { UserView } from "@/types/api";
import { formatDateTime, formatLongDate, titleCase } from "@/utils/format";

const pct = (value: number | null | undefined) => (value == null ? "—" : `${value.toFixed(1)}%`);

function DepartmentFilter({ value, onChange }: { value: string; onChange: (code: string) => void }) {
  const { data } = useAdminDepartments();
  return (
    <label className="flex items-center gap-2 text-sm">
      <span className="text-muted">Department</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-9 rounded-lg border border-border bg-surface px-2 text-sm focus:border-accent focus:outline-none"
        aria-label="Department filter"
      >
        <option value="">All departments</option>
        {data?.map((d) => (
          <option key={d.code} value={d.code}>
            {d.code} — {d.name}
          </option>
        ))}
      </select>
    </label>
  );
}

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------

export function AdminDashboardPage() {
  const [department, setDepartment] = useState("");
  const { data, isLoading, isError, error, refetch } = useAdminDashboard(department || undefined);
  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-accent-hover">Institution</p>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight">Campus operations</h1>
          <p className="mt-1 text-sm text-muted">
            {formatLongDate()}
            {data && ` · ${data.now_local} IST`}
          </p>
        </div>
        <DepartmentFilter value={department} onChange={setDepartment} />
      </header>
      {isError && <ErrorState message={`CampusNexus couldn't load institution metrics. ${(error as Error).message}`} onRetry={() => void refetch()} />}
      {data && (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-3 xl:grid-cols-5">
          <HodStat label="Total students" icon={<GraduationCap />} value={data.total_students} detail={`${data.departments} department${data.departments === 1 ? "" : "s"}`} />
          <HodStat label="Total faculty" icon={<Users />} value={data.total_faculty} detail="Teaching staff" />
          <HodStat label="Departments" icon={<Building2 />} value={data.departments} detail="Open the overview" to="/admin/departments" />
          <HodStat label="Classes today" icon={<CalendarDays />} value={data.classes_today} detail={`${data.completed_classes} completed · ${data.cancelled_classes} cancelled`} />
          <HodStat label="Active classes" icon={<Radio />} value={data.active_classes} detail={`${data.not_started_classes} not started`} danger={data.not_started_classes > 0} />
          <HodStat label="Pending requests" icon={<ClipboardList />} value={data.pending_requests} detail="Anywhere in the institution" to="/admin/requests" />
          <HodStat label="Escalations" icon={<AlertTriangle />} value={data.escalations} detail="Escalated or needing review" to="/admin/requests" danger={data.escalations > 0} />
          <HodStat label="SLA breaches" icon={<ShieldAlert />} value={data.sla_breaches} detail="Open complaints past SLA" to="/admin/complaints" danger={data.sla_breaches > 0} />
          <HodStat label="Agent/system errors" icon={<Cpu />} value={data.system_errors_24h} detail="Last 24 hours" to="/admin/ai-operations" danger={data.system_errors_24h > 0} />
        </div>
      )}
      <ActivityList classes={data?.activity} graceMinutes={data?.start_grace_minutes} isLoading={isLoading} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Departments
// ---------------------------------------------------------------------------

export function AdminDepartmentsPage() {
  const { data, isLoading, isError, error, refetch } = useAdminDepartments();
  return (
    <div className="space-y-6">
      <PageTitle title="Departments" description="Every department's leadership, size, today's classes, attendance risk, requests and complaints." />
      {isLoading && <SkeletonRows rows={4} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <Card>
          <CardBody>
            <DataTable columns={["Department", "HOD", "Faculty", "Students", "Classes today", "Attendance risk", "Pending requests", "Complaints", ""]}>
              {data.map((d) => (
                <tr key={d.code} className="border-b border-border last:border-0">
                  <td className={cell}>
                    <div className="font-medium">{d.code}</div>
                    <div className="text-xs text-muted">{d.name}</div>
                  </td>
                  <td className={cell}>{d.hod_name ?? <span className="text-xs text-muted">Not recorded</span>}</td>
                  <td className={`${cell} tabular-nums`}>{d.faculty_count}</td>
                  <td className={`${cell} tabular-nums`}>{d.student_count}</td>
                  <td className={`${cell} tabular-nums`}>
                    {d.classes_today}
                    {d.not_started_classes > 0 && <Badge tone="danger" className="ml-2">{d.not_started_classes} not started</Badge>}
                  </td>
                  <td className={cell}>{d.attendance_risk_students ? <Badge tone="danger">{d.attendance_risk_students}</Badge> : "0"}</td>
                  <td className={`${cell} tabular-nums`}>{d.pending_requests}</td>
                  <td className={cell}>
                    {d.open_complaints} open
                    {d.sla_breaches > 0 && <Badge tone="danger" className="ml-2">{d.sla_breaches} past SLA</Badge>}
                  </td>
                  <td className={cell}>
                    <Link to={`/admin/departments/${d.code}`} className="inline-flex items-center gap-1 text-xs font-medium text-accent-hover">
                      Open <ArrowRight className="size-3.5" />
                    </Link>
                  </td>
                </tr>
              ))}
            </DataTable>
          </CardBody>
        </Card>
      )}
    </div>
  );
}

export function AdminDepartmentPage() {
  const { code = "" } = useParams();
  const { data, isLoading, isError, error, refetch } = useAdminDepartment(code);
  return (
    <div className="space-y-6">
      <Link to="/admin/departments" className="text-sm text-muted hover:text-ink">
        ← Departments
      </Link>
      {isLoading && <SkeletonRows rows={5} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <>
          <PageTitle title={`${data.department.code} — ${data.department.name}`} description={`HOD: ${data.department.hod_name ?? "not recorded"}. Read-only view.`} />
          <div className="grid grid-cols-2 gap-4 xl:grid-cols-5">
            <HodStat label="Faculty" icon={<Users />} value={data.department.faculty_count} detail="In the department" />
            <HodStat label="Students" icon={<GraduationCap />} value={data.department.student_count} detail="Enrolled" />
            <HodStat label="Classes today" icon={<CalendarDays />} value={data.department.classes_today} detail={`${data.department.not_started_classes} not started`} />
            <HodStat label="Attendance risk" icon={<ShieldAlert />} value={data.department.attendance_risk_students} detail="Students below requirement" danger={data.department.attendance_risk_students > 0} />
            <HodStat label="SLA breaches" icon={<AlertTriangle />} value={data.department.sla_breaches} detail={`${data.department.open_complaints} open complaints`} danger={data.department.sla_breaches > 0} />
          </div>
          <ActivityList classes={data.activity} />
          <div className="grid gap-6 xl:grid-cols-2">
            <Card>
              <CardHeader icon={<Users />} title="Faculty" />
              <CardBody>
                <DataTable columns={["Faculty", "Classes today", "Now"]}>
                  {data.faculty.map((f) => (
                    <tr key={f.faculty_id} className="border-b border-border last:border-0">
                      <td className={cell}>
                        <div className="font-medium">
                          {f.full_name} {f.is_hod && <Badge tone="primary">HOD</Badge>}
                        </div>
                        <div className="text-xs text-muted">{f.designation}</div>
                      </td>
                      <td className={`${cell} tabular-nums`}>{f.classes_today}</td>
                      <td className={cell}>{f.active_class ? <Badge tone="accent">{f.active_class}</Badge> : <span className="text-xs text-muted">—</span>}</td>
                    </tr>
                  ))}
                </DataTable>
              </CardBody>
            </Card>
            <Card>
              <CardHeader icon={<ShieldAlert />} title="Students below the requirement" />
              <CardBody>
                {data.at_risk_students.length === 0 ? (
                  <EmptyState title="Nobody is below the requirement" />
                ) : (
                  <DataTable columns={["Student", "Overall", "Courses below"]}>
                    {data.at_risk_students.map((s) => (
                      <tr key={s.student_id} className="border-b border-border last:border-0">
                        <td className={cell}>
                          <div className="font-medium">{s.full_name}</div>
                          <div className="text-xs text-muted">
                            {s.student_id} · Year {s.year}
                          </div>
                        </td>
                        <td className={`${cell} tabular-nums`}>{pct(s.overall_percentage)}</td>
                        <td className={cell}>{s.courses_below_threshold.join(", ")}</td>
                      </tr>
                    ))}
                  </DataTable>
                )}
              </CardBody>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Users & roles
// ---------------------------------------------------------------------------

function UserActions({ user, self }: { user: UserView; self: boolean }) {
  const client = useQueryClient();
  const [password, setPassword] = useState<string | null>(null);
  const refresh = () => void client.invalidateQueries({ queryKey: queryKeys.admin("users") });
  const active = useMutation({ mutationFn: () => api.adminSetActive(user.account_id, !user.is_active), onSuccess: refresh });
  const role = useMutation({ mutationFn: (next: string) => api.adminSetRole(user.account_id, next), onSuccess: refresh });
  const reset = useMutation({ mutationFn: () => api.adminResetPassword(user.account_id), onSuccess: (r) => setPassword(r.temporary_password) });
  const error = active.error ?? role.error ?? reset.error;
  if (self) return <span className="text-xs text-muted">Your account</span>;
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-2">
        {user.allowed_roles.length > 1 && (
          <select
            aria-label={`Role for ${user.email}`}
            value={user.role}
            onChange={(event) => role.mutate(event.target.value)}
            disabled={role.isPending}
            className="h-8 rounded-lg border border-border bg-surface px-2 text-xs"
          >
            {user.allowed_roles.map((r) => (
              <option key={r} value={r}>
                {titleCase(r)}
              </option>
            ))}
          </select>
        )}
        <Button variant="outline" size="sm" onClick={() => active.mutate()} disabled={active.isPending}>
          {user.is_active ? "Deactivate" : "Activate"}
        </Button>
        <Button variant="ghost" size="sm" onClick={() => reset.mutate()} disabled={reset.isPending}>
          <KeyRound /> Reset password
        </Button>
      </div>
      {password && (
        <Notice tone="warning" className="text-xs">
          Temporary password (shown once): <code className="font-semibold">{password}</code>
        </Notice>
      )}
      {error && <p className="text-xs text-danger-strong">{error instanceof ApiError ? error.message : "That didn't work."}</p>}
    </div>
  );
}

export function AdminUsersPage() {
  const { user: me } = useAuth();
  const { data, isLoading, isError, error, refetch } = useAdminUsers();
  return (
    <div className="space-y-6">
      <PageTitle title="Users & Roles" description="Accounts, their linked profiles and state. Roles can only change within valid profile links." />
      {isLoading && <SkeletonRows rows={6} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <Card>
          <CardBody>
            <DataTable columns={["User", "Role", "Linked profile", "Department", "State", "Last sign-in", "Actions"]}>
              {data.map((u) => (
                <tr key={u.account_id} className="border-b border-border last:border-0">
                  <td className={cell}>
                    <div className="font-medium">{u.display_name}</div>
                    <div className="text-xs text-muted">{u.email}</div>
                  </td>
                  <td className={cell}>
                    <Badge tone="primary">{titleCase(u.role)}</Badge>
                    {u.is_department_head && <div className="mt-1 text-[11px] text-muted">Department head</div>}
                  </td>
                  <td className={`${cell} text-xs`}>{u.linked_faculty ?? u.linked_student_id ?? "—"}</td>
                  <td className={cell}>{u.department_code ?? "—"}</td>
                  <td className={cell}>
                    <Badge tone={u.is_active ? "success" : "neutral"}>{u.is_active ? "Active" : "Inactive"}</Badge>
                  </td>
                  <td className={`${cell} text-xs`}>{u.last_login_at ? formatDateTime(u.last_login_at) : "Never"}</td>
                  <td className={cell}>
                    <UserActions user={u} self={me?.id === u.account_id} />
                  </td>
                </tr>
              ))}
            </DataTable>
          </CardBody>
        </Card>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Attendance
// ---------------------------------------------------------------------------

export function AdminAttendancePage() {
  const [department, setDepartment] = useState("");
  const { data, isLoading, isError, error, refetch } = useAdminAttendance(department || undefined);
  return (
    <div className="space-y-6">
      <PageTitle
        title="Attendance"
        description={data?.required_percentage != null ? `Institution attendance against the ${data.required_percentage}% policy requirement. Factual metrics only.` : "Institution attendance."}
        action={<DepartmentFilter value={department} onChange={setDepartment} />}
      />
      {isLoading && <SkeletonRows rows={6} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <>
          <Card>
            <CardHeader icon={<Building2 />} title="By department" description={`${data.low_attendance_students} students below the requirement in at least one course`} />
            <CardBody>
              <DataTable columns={["Department", "Students", "Attended", "Attendance", "Below requirement"]}>
                {data.by_department.map((d) => (
                  <tr key={d.code} className="border-b border-border last:border-0">
                    <td className={cell}>
                      <div className="font-medium">{d.code}</div>
                      <div className="text-xs text-muted">{d.name}</div>
                    </td>
                    <td className={`${cell} tabular-nums`}>{d.students}</td>
                    <td className={`${cell} tabular-nums`}>{d.classes_attended}/{d.classes_conducted}</td>
                    <td className={`${cell} tabular-nums`}>{pct(d.percentage)}</td>
                    <td className={cell}>{d.students_below_threshold ? <Badge tone="danger">{d.students_below_threshold}</Badge> : "0"}</td>
                  </tr>
                ))}
              </DataTable>
            </CardBody>
          </Card>
          <div className="grid gap-6 xl:grid-cols-2">
            <Card>
              <CardHeader title="By course" />
              <CardBody>
                <DataTable columns={["Course", "Section", "Faculty", "Attendance", "Below"]}>
                  {data.insights.courses.map((c) => (
                    <tr key={c.assignment_id} className="border-b border-border last:border-0">
                      <td className={cell}>
                        <div className="font-medium">{c.course_title}</div>
                        <div className="text-xs text-muted">{c.course_code}</div>
                      </td>
                      <td className={cell}>{c.class_label}</td>
                      <td className={`${cell} text-xs`}>{c.faculty_name}</td>
                      <td className={`${cell} tabular-nums`}>{pct(c.percentage)}</td>
                      <td className={cell}>{c.below_threshold || "0"}</td>
                    </tr>
                  ))}
                </DataTable>
              </CardBody>
            </Card>
            <Card>
              <CardHeader title="By section" />
              <CardBody>
                <DataTable columns={["Section", "Students", "Attendance", "Below"]}>
                  {data.insights.sections.map((s) => (
                    <tr key={s.class_label} className="border-b border-border last:border-0">
                      <td className={`${cell} font-medium`}>{s.class_label}</td>
                      <td className={`${cell} tabular-nums`}>{s.students}</td>
                      <td className={`${cell} tabular-nums`}>{pct(s.percentage)}</td>
                      <td className={cell}>{s.below_threshold || "0"}</td>
                    </tr>
                  ))}
                </DataTable>
              </CardBody>
            </Card>
          </div>
          <Card>
            <CardHeader title="Active and incomplete sessions" description="Live sessions right now; incomplete = students not yet marked" />
            <CardBody>
              {data.active_sessions.length === 0 ? (
                <EmptyState title="No class is in session right now" />
              ) : (
                <DataTable columns={["Class", "Faculty", "Started", "Present", "Not marked"]}>
                  {data.active_sessions.map((s) => (
                    <tr key={s.session_id} className="border-b border-border last:border-0">
                      <td className={cell}>
                        {s.course_title} <span className="text-xs text-muted">{s.class_label}</span>
                      </td>
                      <td className={`${cell} text-xs`}>{s.faculty_name}</td>
                      <td className={`${cell} text-xs`}>{s.actual_started_at ? formatDateTime(s.actual_started_at) : "—"}</td>
                      <td className={`${cell} tabular-nums`}>{s.tally.present + s.tally.late}/{s.tally.roster}</td>
                      <td className={cell}>{s.tally.unmarked ? <Badge tone="warning">{s.tally.unmarked}</Badge> : "0"}</td>
                    </tr>
                  ))}
                </DataTable>
              )}
            </CardBody>
          </Card>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Requests, complaints
// ---------------------------------------------------------------------------

export function AdminRequestsPage() {
  return (
    <div className="max-w-5xl space-y-6">
      <PageTitle title="Requests" description="Requests routed to the administration: HOD requests and anything no one below could take. Routing history is shown on each." />
      <ReviewerInbox
        groups={[
          { title: "HOD requests", filter: (r) => r.requester_role === "hod" },
          { title: "Escalated requests", filter: (r) => r.requester_role !== "hod" },
        ]}
      />
    </div>
  );
}

export function AdminComplaintsPage() {
  const [department, setDepartment] = useState("");
  const [status, setStatus] = useState("");
  const [breached, setBreached] = useState(false);
  const { data, isLoading, isError, error, refetch } = useAdminComplaints({ department: department || undefined, status: status || undefined, breached });
  return (
    <div className="space-y-6">
      <PageTitle title="Complaints" description="Institution-wide complaints and their SLA state. Read-only; cases change only through the controlled complaint workflow." />
      <div className="flex flex-wrap items-center gap-4">
        <DepartmentFilter value={department} onChange={setDepartment} />
        <label className="flex items-center gap-2 text-sm">
          <span className="text-muted">Status</span>
          <select aria-label="Status filter" value={status} onChange={(e) => setStatus(e.target.value)} className="h-9 rounded-lg border border-border bg-surface px-2 text-sm">
            <option value="">Any</option>
            <option value="open">Open</option>
            <option value="in_progress">In progress</option>
            <option value="resolved">Resolved</option>
            <option value="closed">Closed</option>
          </select>
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={breached} onChange={(e) => setBreached(e.target.checked)} /> SLA breached only
        </label>
      </div>
      {isLoading && <SkeletonRows rows={5} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <Card>
          <CardHeader icon={<ShieldAlert />} title={`${data.length} complaints`} description={`${data.filter((c) => (c.response_breached || c.resolution_breached) && (c.status === "open" || c.status === "in_progress")).length} open complaints past SLA`} />
          <CardBody>
            {data.length === 0 ? (
              <EmptyState title="No complaints match" />
            ) : (
              <DataTable columns={["Case", "Department", "Office", "Priority", "Status", "Response SLA", "Resolution SLA"]}>
                {data.map((c) => (
                  <tr key={c.case_code} className="border-b border-border last:border-0">
                    <td className={cell}>
                      <div className="font-medium">{c.case_code}</div>
                      <div className="text-xs text-muted">{c.category.replace(/_/g, " ")}</div>
                    </td>
                    <td className={cell}>{c.student_department}</td>
                    <td className={`${cell} text-xs`}>{c.office}</td>
                    <td className={cell}>{titleCase(c.priority)}</td>
                    <td className={cell}>
                      <Badge tone="neutral">{c.status.replace(/_/g, " ")}</Badge>
                    </td>
                    <td className={cell}>{c.response_breached ? <Badge tone="danger">Breached</Badge> : <span className="text-xs text-muted">{c.response_due_at ? formatDateTime(c.response_due_at) : "—"}</span>}</td>
                    <td className={cell}>{c.resolution_breached ? <Badge tone="danger">Breached</Badge> : <span className="text-xs text-muted">{c.resolution_due_at ? formatDateTime(c.resolution_due_at) : "—"}</span>}</td>
                  </tr>
                ))}
              </DataTable>
            )}
          </CardBody>
        </Card>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// AI operations, audit log
// ---------------------------------------------------------------------------

export function AdminAIOperationsPage() {
  const { data, isLoading, isError, error, refetch } = useAdminAIOperations();
  return (
    <div className="space-y-6">
      <PageTitle title="AI Operations" description="Provider, missions, agent runs, approvals and failures, from the audit trail. Nothing here is estimated." />
      {isLoading && <SkeletonRows rows={6} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <>
          {!data.live && <Notice tone="warning">LLM provider is the offline mock ({data.provider}). Answers are deterministic, not live AI.</Notice>}
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <HodStat label="LLM provider" icon={<Cpu />} value={data.provider} detail={data.model ?? (data.live ? "live" : "mock mode")} />
            <HodStat label="Failed missions" icon={<AlertTriangle />} value={data.failed_missions} detail={`${Object.values(data.missions_by_status).reduce((a, b) => a + b, 0)} missions in total`} danger={data.failed_missions > 0} />
            <HodStat label="Agent runs" icon={<ScrollText />} value={data.agent_runs_total} detail={`${data.agent_runs_failed} failed`} />
            <HodStat label="Provider errors" icon={<Radio />} value={data.provider_errors} detail={`${data.rate_limit_incidents} rate-limit incidents`} danger={data.provider_errors > 0} />
            <HodStat label="Pending approvals" icon={<ClipboardList />} value={data.pending_approvals} detail={`${data.stale_approvals} stale`} />
            <HodStat label="Workflow failures" icon={<AlertTriangle />} value={data.workflow_failures} detail={`${data.requests_needing_review} requests need review`} />
            <HodStat label="RAG" icon={<ScrollText />} value={data.rag_ready ? "Ready" : "Unavailable"} detail={`${data.rag_chunks} policy chunks`} danger={!data.rag_ready} />
            <HodStat label="Database" icon={<Building2 />} value={data.database_ready ? "Ready" : "Unavailable"} detail={`Token usage: ${data.token_usage}`} danger={!data.database_ready} />
          </div>
          <Card>
            <CardHeader title="Recent missions" />
            <CardBody>
              {data.recent_missions.length === 0 ? (
                <EmptyState title="No missions yet" />
              ) : (
                <DataTable columns={["Mission", "Goal", "Status", "Updated"]}>
                  {data.recent_missions.map((m) => (
                    <tr key={m.mission_id} className="border-b border-border last:border-0">
                      <td className={`${cell} text-xs`}>{m.mission_id}</td>
                      <td className={`${cell} text-xs`}>{m.goal}</td>
                      <td className={cell}>
                        <Badge tone={m.status === "failed" ? "danger" : m.status === "completed" ? "success" : "neutral"}>{m.status.replace(/_/g, " ")}</Badge>
                      </td>
                      <td className={`${cell} text-xs`}>{formatDateTime(m.updated_at)}</td>
                    </tr>
                  ))}
                </DataTable>
              )}
            </CardBody>
          </Card>
          <div className="grid gap-6 xl:grid-cols-2">
            {[
              { title: "Recent provider errors", rows: data.recent_provider_errors },
              { title: "Recent workflow failures", rows: data.recent_workflow_failures },
            ].map((block) => (
              <Card key={block.title}>
                <CardHeader title={block.title} />
                <CardBody className="space-y-2">
                  {block.rows.length === 0 && <EmptyState title="None recorded" />}
                  {block.rows.map((e, i) => (
                    <div key={`${e.reference}-${i}`} className="rounded-lg border border-border px-3 py-2 text-sm">
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-medium">{e.event_type.replace(/_/g, " ")}</span>
                        <span className="text-[11px] text-subtle">{formatDateTime(e.timestamp)}</span>
                      </div>
                      <p className="mt-0.5 text-xs text-muted">{e.message}</p>
                    </div>
                  ))}
                </CardBody>
              </Card>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export function AdminAuditPage() {
  const [source, setSource] = useState("");
  const [action, setAction] = useState("");
  const { data, isLoading, isError, error, refetch } = useAdminAudit({ source: source || undefined, action: action || undefined });
  return (
    <div className="space-y-6">
      <PageTitle title="Audit Log" description="Append-only mission and operations audit trails, newest first. Read-only." />
      <div className="flex flex-wrap items-center gap-4">
        <label className="flex items-center gap-2 text-sm">
          <span className="text-muted">Source</span>
          <select aria-label="Source filter" value={source} onChange={(e) => setSource(e.target.value)} className="h-9 rounded-lg border border-border bg-surface px-2 text-sm">
            <option value="">All</option>
            <option value="operations">Operations</option>
            <option value="mission">Missions & agents</option>
          </select>
        </label>
        <label className="flex items-center gap-2 text-sm">
          <span className="text-muted">Action contains</span>
          <input
            aria-label="Action filter"
            value={action}
            onChange={(e) => setAction(e.target.value)}
            placeholder="e.g. request, class, login"
            className="h-9 rounded-lg border border-border bg-surface px-2 text-sm"
          />
        </label>
      </div>
      {isLoading && <SkeletonRows rows={8} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <Card>
          <CardBody>
            {data.length === 0 ? (
              <EmptyState title="No audit entries match" />
            ) : (
              <DataTable columns={["Time", "Actor", "Role", "Action", "Target", "Reference", "Outcome"]}>
                {data.map((e, i) => (
                  <tr key={`${e.timestamp}-${i}`} className="border-b border-border last:border-0">
                    <td className={`${cell} whitespace-nowrap text-xs`}>{formatDateTime(e.timestamp)}</td>
                    <td className={cell}>{e.actor}</td>
                    <td className={`${cell} text-xs`}>{e.role}</td>
                    <td className={cell}>
                      <Badge tone={e.source === "mission" ? "info" : "primary"}>{e.action.replace(/_/g, " ")}</Badge>
                    </td>
                    <td className={`${cell} text-xs`}>{e.target.replace(/_/g, " ")}</td>
                    <td className={`${cell} text-xs`}>{e.reference}</td>
                    <td className={`${cell} text-xs text-muted`}>{e.outcome}</td>
                  </tr>
                ))}
              </DataTable>
            )}
          </CardBody>
        </Card>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Agents, settings
// ---------------------------------------------------------------------------

export function AdminAgentsPage() {
  return (
    <div className="space-y-6">
      <PageTitle title="Agents" description="Institution-scoped agents. They read records across every department and never change anything." />
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {ADMIN_AGENTS.map((agent) => (
          <Link key={agent.key} to={`/admin/agents/${agent.key}`} className="block rounded-[var(--radius-card)]">
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
              </div>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  );
}

export function AdminAgentPage() {
  const { agentKey } = useParams();
  const { user } = useAuth();
  const agent = adminAgentByKey(agentKey);
  if (!agent) return <Navigate to="/admin/agents" replace />;
  if (!user) return null;
  return <AgentWorkspace key={agent.key} agent={agent} userId={user.id} scope="admin" />;
}

const SETTING_LABELS: Record<string, string> = {
  llm_provider: "LLM provider",
  llm_model: "Model",
  llm_live: "Live AI",
  live_ai_key_configured: "API key configured",
  class_start_grace_minutes: "Class-start grace period (minutes)",
  embedding_provider: "Embedding provider",
  database_mode: "Database",
  vector_store_path: "Vector store",
  jwt_secret_configured: "JWT secret configured",
  jwt_ttl_minutes: "Session length (minutes)",
};

function settingValue(value: unknown): string {
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (value && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([k, v]) => `${k}: ${v ? "yes" : "no"}`)
      .join(", ");
  }
  return value == null ? "—" : String(value);
}

export function AdminSettingsPage() {
  const { user } = useAuth();
  const { data, isLoading, isError, error, refetch } = useAdminSystem();
  return (
    <div className="max-w-3xl space-y-6">
      <PageTitle title="Settings" description="System health and a safe configuration summary. Secrets are never shown — only whether they are set." />
      {isLoading && <SkeletonRows rows={5} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <>
          <Card>
            <CardHeader title="System health" description={data.overall === "ready" ? "All components ready" : "Degraded — see below"} action={<Badge tone={data.overall === "ready" ? "success" : "warning"}>{data.overall}</Badge>} />
            <CardBody className="space-y-2">
              {data.components.map((c) => (
                <div key={c.name} className="flex items-center justify-between gap-3 rounded-lg border border-border px-4 py-2.5">
                  <div>
                    <div className="text-sm font-medium">{c.name}</div>
                    <div className="text-xs text-muted">{c.detail}</div>
                  </div>
                  <Badge tone={c.status === "ready" ? "success" : c.status === "degraded" ? "warning" : "danger"}>{c.status}</Badge>
                </div>
              ))}
            </CardBody>
          </Card>
          <Card>
            <CardHeader title="Configuration" />
            <CardBody>
              {Object.entries(data.settings).map(([key, value]) => (
                <div key={key} className="flex justify-between gap-4 border-b border-dashed border-border py-2.5 text-sm last:border-0">
                  <span className="text-muted">{SETTING_LABELS[key] ?? titleCase(key)}</span>
                  <span className="text-right font-medium">{settingValue(value)}</span>
                </div>
              ))}
            </CardBody>
          </Card>
        </>
      )}
      {user && (
        <Card>
          <CardHeader title="Account" />
          <CardBody className="text-sm">
            {user.display_name} · {user.email}
          </CardBody>
        </Card>
      )}
    </div>
  );
}
