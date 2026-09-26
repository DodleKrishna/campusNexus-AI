import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, Building2, CalendarDays, GraduationCap, KeyRound, Radio, ScrollText, ShieldAlert, UserCog, Users } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError } from "@/api/client";
import { api, queryKeys } from "@/api/endpoints";
import { useAuth } from "@/auth/useAuth";
import { AgentDirectory } from "@/components/agents/AgentCards";
import { AskAgentLink } from "@/components/agents/AskAgentLink";
import { RoleAgentPage } from "@/components/agents/RoleAgentPage";
import { COMPONENT_HEALTH, MISSION_STATUS, statusOf } from "@/components/dashboard/status";
import { ActivityList, DepartmentPulse } from "@/components/hod/ActivityList";
import { FacultyTable } from "@/components/hod/DepartmentTables";
import { Avatar } from "@/components/ui/avatar";
import { ActivityFeed, type ActivityItem } from "@/components/ui/activity-feed";
import { activityCategory } from "@/utils/activityCategory";
import { Badge, StatusBadge, StatusDot } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { CellTitle, DataTable } from "@/components/ui/data-table";
import { DetailList, DetailRow } from "@/components/ui/detail-list";
import { FormSection } from "@/components/ui/form-section";
import { Label, SearchInput, Select, Toolbar } from "@/components/ui/input";
import { MetricCard, MetricGrid, MetricSkeletons } from "@/components/ui/metric-card";
import { Sheet } from "@/components/ui/sheet";
import { SkeletonRows, SkeletonTable } from "@/components/ui/skeleton";
import { EmptyState, ErrorState, Notice } from "@/components/ui/states";
import { TabPanel, Tabs } from "@/components/ui/tabs";
import { ADMIN_AGENTS, adminAgentByKey } from "@/features/agents/adminCatalog";
import { AgentRuns, DeployableCatalog, DeployedAgents, LifecycleStrip } from "@/features/admin/AgentProducts";
import { AgentCatalog, ControlTower, IntelligencePanel } from "@/features/admin/IntelligenceOps";
import { ReviewerInbox } from "@/features/requests/ReviewerInbox";
import {
  useAdminAIOperations,
  useAdminAgentCatalog,
  useAdminAgentsCatalog,
  useAdminDeployments,
  useAdminAttendance,
  useAdminAudit,
  useAdminComplaints,
  useAdminDashboard,
  useAdminDepartment,
  useAdminDepartments,
  useAdminSystem,
  useAdminUsers,
} from "@/hooks/useAdminData";
import { DashboardHeader, PageTitle } from "@/pages/PageTitle";
import type { AuditEntry, Role, UserView } from "@/types/api";
import { ROLE_LABELS } from "@/auth/roles";
import { cn } from "@/utils/cn";
import { rowKeys } from "@/utils/rowKeys";
import { formatDateTime, formatLongDate, greeting, titleCase } from "@/utils/format";

const roleLabel = (role: string) => ROLE_LABELS[role as Role] ?? titleCase(role);
const pct = (value: number | null | undefined) => (value == null ? "—" : `${value.toFixed(1)}%`);

function DepartmentFilter({ value, onChange }: { value: string; onChange: (code: string) => void }) {
  const { data } = useAdminDepartments();
  return (
    <Select value={value} onChange={(event) => onChange(event.target.value)} aria-label="Department filter" className="w-full sm:w-64">
      <option value="">All departments</option>
      {data?.map((d) => (
        <option key={d.code} value={d.code}>
          {d.code} — {d.name}
        </option>
      ))}
    </Select>
  );
}

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------

/** Real audit entries (operations + missions) as a live campus activity feed. */
function LiveCampusActivity() {
  const { data, isLoading, isError, refetch } = useAdminAudit({});
  const items: ActivityItem[] =
    data
      ?.filter((e) => e.action !== "login" && e.action !== "logout")
      .slice(0, 8)
      .map((e, i) => ({
      id: `${e.timestamp}-${i}`,
      category: activityCategory(e.action, e.source),
      title: e.outcome || titleCase(e.action),
      context: `${e.actor}${e.role ? ` · ${roleLabel(e.role)}` : ""}`,
      time: e.timestamp,
    })) ?? [];
  return (
    <Card className="overflow-hidden">
      <CardHeader
        title="Live campus activity"
        description="From the audit trail"
        action={
          <Link to="/admin/audit" className="text-[13px] font-medium text-primary hover:underline">
            Audit log
          </Link>
        }
      />
      {isLoading && <SkeletonRows rows={5} className="px-5 pb-5" />}
      {isError && (
        <div className="px-5 pb-5">
          <ErrorState message="The activity feed is not available right now." onRetry={() => void refetch()} />
        </div>
      )}
      {data && items.length === 0 && <EmptyState compact title="No activity recorded yet" description="Sign-ins, classes, requests and agent runs appear here." />}
      {items.length > 0 && <ActivityFeed items={items} className="border-t border-border" />}
    </Card>
  );
}

function SystemHealthPanel() {
  const { data, isLoading, isError } = useAdminSystem();
  return (
    <Card>
      <CardHeader
        title="System health"
        action={
          <Link to="/admin/ai-operations" className="text-[13px] font-medium text-primary hover:underline">
            View details
          </Link>
        }
      />
      {isLoading && <SkeletonRows rows={2} className="px-5 pb-5" />}
      {isError && <p className="px-5 pb-4 text-sm text-muted">System status is not available right now.</p>}
      {data && (
        <ul className="divide-y divide-border border-t border-border">
          {data.components.map((c) => (
            <li key={c.name} className="flex items-center justify-between gap-3 px-5 py-2.5">
              <span className="min-w-0 truncate text-sm text-ink" title={c.detail}>
                {c.name}
              </span>
              <StatusBadge {...statusOf(COMPONENT_HEALTH, c.status)} />
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function StatusRow({ label, value, tone, to }: { label: string; value: number; tone: "live" | "caution" | "warning" | "danger"; to: string }) {
  const TEXT = { live: "text-primary", caution: "text-caution-strong", warning: "text-warning-strong", danger: "text-danger-strong" };
  return (
    <li>
      <Link to={to} className="flex items-center justify-between gap-3 px-5 py-3 transition-colors hover:bg-surface-muted/60">
        <span className="flex items-center gap-2.5 text-sm text-ink">
          <StatusDot tone={value ? tone : "neutral"} pulse={tone === "live" && value > 0} />
          {label}
        </span>
        <span className={cn("text-base font-semibold tabular-nums", value ? TEXT[tone] : "text-muted")}>{value}</span>
      </Link>
    </li>
  );
}

export function AdminDashboardPage() {
  const { user } = useAuth();
  const [department, setDepartment] = useState("");
  const { data, isLoading, isError, error, refetch } = useAdminDashboard(department || undefined);
  return (
    <div className="space-y-6">
      <DashboardHeader
        title="Campus overview"
        context={user ? `${greeting()}, ${user.display_name.split(" ")[0]}` : undefined}
        date={
          <>
            {formatLongDate()}
            {data && ` · ${data.now_local} IST`}
          </>
        }
        aside={<DepartmentFilter value={department} onChange={setDepartment} />}
      />
      {isError && <ErrorState message={`CampusNexus couldn't load institution metrics. ${(error as Error).message}`} onRetry={() => void refetch()} />}
      {isLoading && <MetricSkeletons />}
      {data && (
        <MetricGrid>
          <MetricCard label="Students" icon={<GraduationCap />} value={data.total_students} context={`${data.departments} department${data.departments === 1 ? "" : "s"}`} />
          <MetricCard label="Faculty" icon={<Users />} value={data.total_faculty} context="Teaching staff" />
          <MetricCard label="Departments" icon={<Building2 />} value={data.departments} context="View departments" to="/admin/departments" />
          <MetricCard label="Classes today" icon={<CalendarDays />} value={data.classes_today} context={`${data.completed_classes} completed · ${data.cancelled_classes} cancelled`} />
        </MetricGrid>
      )}
      <div className="grid grid-cols-1 items-start gap-6 xl:grid-cols-[minmax(0,65fr)_minmax(0,35fr)]">
        <LiveCampusActivity />
        <div className="space-y-6">
          <Card>
            <CardHeader title="Operational status" />
            {isLoading && <SkeletonRows rows={4} className="px-5 pb-5" />}
            {data && (
              <ul className="divide-y divide-border border-t border-border">
                <StatusRow label="Active classes" value={data.active_classes} tone="live" to="/admin/attendance" />
                <StatusRow label="Delayed classes" value={data.not_started_classes} tone="caution" to="/admin/attendance" />
                <StatusRow label="Pending requests" value={data.pending_requests} tone="warning" to="/admin/requests" />
                <StatusRow label="SLA breaches" value={data.sla_breaches} tone="danger" to="/admin/complaints" />
              </ul>
            )}
          </Card>
          <SystemHealthPanel />
        </div>
      </div>
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
      <PageTitle title="Departments" description="Each department's leadership, size, today's classes, attendance risk and complaints." />
      {isLoading && <SkeletonTable rows={4} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <Card className="overflow-hidden">
          {data.length === 0 ? (
            <EmptyState icon={<Building2 />} title="No departments yet" />
          ) : (
            <DataTable columns={["Department", "HOD", "Faculty", "Students", "Classes today", "At risk", "Complaints", ""]} caption="Departments">
              {data.map((d) => (
                <tr key={d.code}>
                  <td>
                    <CellTitle title={d.code} subtitle={d.name} />
                  </td>
                  <td>{d.hod_name ?? <span className="text-muted">Not recorded</span>}</td>
                  <td className="tabular-nums">{d.faculty_count}</td>
                  <td className="tabular-nums">{d.student_count}</td>
                  <td className="tabular-nums">
                    <span className="inline-flex flex-wrap items-center justify-end gap-2">
                      {d.classes_today}
                      {d.not_started_classes > 0 && <StatusBadge label={`${d.not_started_classes} not started`} tone="warning" />}
                    </span>
                  </td>
                  <td>{d.attendance_risk_students ? <StatusBadge label={String(d.attendance_risk_students)} tone="danger" /> : <span className="text-muted">0</span>}</td>
                  <td>
                    <span className="inline-flex flex-wrap items-center justify-end gap-2">
                      <span className="tabular-nums">{d.open_complaints} open</span>
                      {d.sla_breaches > 0 && <StatusBadge label={`${d.sla_breaches} past SLA`} tone="danger" />}
                    </span>
                  </td>
                  <td>
                    <Link to={`/admin/departments/${d.code}`} className="inline-flex items-center gap-1 text-sm font-medium text-accent-hover hover:underline">
                      Open <ArrowRight className="size-3.5" />
                    </Link>
                  </td>
                </tr>
              ))}
            </DataTable>
          )}
        </Card>
      )}
    </div>
  );
}

export function AdminDepartmentPage() {
  const { code = "" } = useParams();
  const { data, isLoading, isError, error, refetch } = useAdminDepartment(code);
  const d = data?.department;
  return (
    <div className="space-y-6">
      <PageTitle
        back={{ to: "/admin/departments", label: "Departments" }}
        title={d ? `${d.code} — ${d.name}` : code}
        description={d ? `HOD: ${d.hod_name ?? "not recorded"} · read-only view` : undefined}
      />
      {isLoading && <SkeletonTable rows={5} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && d && (
        <>
          <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
            <MetricCard label="Faculty" icon={<Users />} value={d.faculty_count} context={`${d.student_count} students`} />
            <MetricCard label="Classes today" icon={<CalendarDays />} value={d.classes_today} context={`${d.not_started_classes} not started`} tone={d.not_started_classes ? "warning" : "default"} />
            <MetricCard label="Attendance risk" icon={<ShieldAlert />} value={d.attendance_risk_students} context="Students below requirement" tone={d.attendance_risk_students ? "danger" : "default"} />
            <MetricCard label="SLA breaches" icon={<AlertTriangle />} value={d.sla_breaches} context={`${d.open_complaints} open complaints`} tone={d.sla_breaches ? "danger" : "default"} />
          </div>
          <DepartmentPulse classes={data.activity} />
          <ActivityList title="Today's classes" classes={data.activity} />
          <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
            <Card className="overflow-hidden">
              <CardHeader title="Faculty" description={`${data.faculty.length} members`} />
              <div className="border-t border-border">
                <FacultyTable faculty={data.faculty} />
              </div>
            </Card>
            <Card className="overflow-hidden">
              <CardHeader title="Students below the requirement" />
              {data.at_risk_students.length === 0 ? (
                <EmptyState compact title="Nobody is below the requirement" />
              ) : (
                <div className="border-t border-border">
                  <DataTable columns={["Student", "Overall", "Courses below"]} caption="Students below the requirement">
                    {data.at_risk_students.map((s) => (
                      <tr key={s.student_id}>
                        <td>
                          <CellTitle title={s.full_name} subtitle={`${s.student_id} · Year ${s.year}`} />
                        </td>
                        <td className="tabular-nums">{pct(s.overall_percentage)}</td>
                        <td>{s.courses_below_threshold.join(", ")}</td>
                      </tr>
                    ))}
                  </DataTable>
                </div>
              )}
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

function UserActions({ user }: { user: UserView }) {
  const client = useQueryClient();
  const [password, setPassword] = useState<string | null>(null);
  const refresh = () => void client.invalidateQueries({ queryKey: queryKeys.admin("users") });
  const active = useMutation({ mutationFn: () => api.adminSetActive(user.account_id, !user.is_active), onSuccess: refresh });
  const role = useMutation({ mutationFn: (next: string) => api.adminSetRole(user.account_id, next), onSuccess: refresh });
  const reset = useMutation({ mutationFn: () => api.adminResetPassword(user.account_id), onSuccess: (r) => setPassword(r.temporary_password) });
  const error = active.error ?? role.error ?? reset.error;
  return (
    <div className="space-y-5">
      {user.allowed_roles.length > 1 && (
        <div className="space-y-1.5">
          <Label htmlFor={`role-${user.account_id}`}>Role</Label>
          <Select
            id={`role-${user.account_id}`}
            aria-label={`Role for ${user.email}`}
            value={user.role}
            onChange={(event) => role.mutate(event.target.value)}
            disabled={role.isPending}
            className="w-full"
          >
            {user.allowed_roles.map((r) => (
              <option key={r} value={r}>
                {titleCase(r)}
              </option>
            ))}
          </Select>
          <p className="text-xs text-muted">Only roles consistent with this account's linked profile are offered.</p>
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        <Button variant={user.is_active ? "danger" : "outline"} size="sm" onClick={() => active.mutate()} disabled={active.isPending}>
          {user.is_active ? "Deactivate account" : "Activate account"}
        </Button>
        <Button variant="outline" size="sm" onClick={() => reset.mutate()} disabled={reset.isPending}>
          <KeyRound /> Reset password
        </Button>
      </div>
      {password && (
        <Notice tone="warning">
          Temporary password (shown once): <code className="font-semibold break-all">{password}</code>
        </Notice>
      )}
      {error && <ErrorState message={error instanceof ApiError ? error.message : "That didn't work."} />}
    </div>
  );
}

export function AdminUsersPage() {
  const { user: me } = useAuth();
  const { data, isLoading, isError, error, refetch } = useAdminUsers();
  const [query, setQuery] = useState("");
  const [roleFilter, setRoleFilter] = useState("");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const q = query.trim().toLowerCase();
  const rows = data?.filter((u) => (!q || u.display_name.toLowerCase().includes(q) || u.email.toLowerCase().includes(q)) && (!roleFilter || u.role === roleFilter)) ?? [];
  const selected = data?.find((u) => u.account_id === selectedId) ?? null;
  return (
    <div className="space-y-6">
      <PageTitle title="Users & Roles" description="Accounts, their linked profiles and state. Roles can only change within valid profile links." />
      <Toolbar>
        <SearchInput value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search name or email" aria-label="Search users" />
        <Select value={roleFilter} onChange={(e) => setRoleFilter(e.target.value)} aria-label="Role filter">
          <option value="">All roles</option>
          {["student", "faculty", "hod", "admin", "staff"].map((r) => (
            <option key={r} value={r}>
              {titleCase(r)}
            </option>
          ))}
        </Select>
        {data && <span className="text-sm text-muted sm:ml-auto">{rows.length} of {data.length} accounts</span>}
      </Toolbar>
      {isLoading && <SkeletonTable rows={6} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <Card className="overflow-hidden">
          {rows.length === 0 ? (
            <EmptyState icon={<UserCog />} title="No accounts match" description="Try a different search or role." />
          ) : (
            <DataTable columns={["User", "Role", "Department", "State", "Last sign-in", ""]} caption="User accounts">
              {rows.map((u) => (
                <tr key={u.account_id}>
                  <td>
                    <div className="flex items-center gap-3">
                      <Avatar name={u.display_name} className="hidden sm:flex" />
                      <CellTitle title={u.display_name} subtitle={u.email} />
                    </div>
                  </td>
                  <td>
                    <Badge tone="primary">{titleCase(u.role)}</Badge>
                  </td>
                  <td>{u.department_code ?? <span className="text-muted">—</span>}</td>
                  <td>
                    <StatusBadge label={u.is_active ? "Active" : "Inactive"} tone={u.is_active ? "success" : "neutral"} />
                  </td>
                  <td className="text-sm text-muted">{u.last_login_at ? formatDateTime(u.last_login_at) : "Never"}</td>
                  <td>
                    {me?.id === u.account_id ? (
                      <span className="text-sm text-muted">You</span>
                    ) : (
                      <Button variant="outline" size="sm" onClick={() => setSelectedId(u.account_id)} aria-label={`Manage ${u.display_name}`}>
                        Manage
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </DataTable>
          )}
        </Card>
      )}
      <Sheet open={selected !== null} onClose={() => setSelectedId(null)} title={selected?.display_name ?? "Account"} description={selected?.email}>
        {selected && (
          <div className="space-y-6 px-5 py-5">
            <DetailList>
              <DetailRow label="Role">{titleCase(selected.role)}</DetailRow>
              <DetailRow label="State">{selected.is_active ? "Active" : "Inactive"}</DetailRow>
              <DetailRow label="Linked profile">{selected.linked_faculty ?? selected.linked_student_id ?? "—"}</DetailRow>
              <DetailRow label="Department">{selected.department_code ?? "—"}</DetailRow>
              {selected.is_department_head && <DetailRow label="Department head">Yes</DetailRow>}
              <DetailRow label="Last sign-in">{selected.last_login_at ? formatDateTime(selected.last_login_at) : "Never"}</DetailRow>
            </DetailList>
            <UserActions key={selected.account_id} user={selected} />
          </div>
        )}
      </Sheet>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Attendance
// ---------------------------------------------------------------------------

export function AdminAttendancePage() {
  const [department, setDepartment] = useState("");
  const [tab, setTab] = useState("departments");
  const { data, isLoading, isError, error, refetch } = useAdminAttendance(department || undefined);
  return (
    <div className="space-y-6">
      <PageTitle
        title="Attendance"
        description={
          data?.required_percentage != null
            ? `Institution attendance against the ${data.required_percentage}% policy requirement · ${data.low_attendance_students} students below it in at least one course.`
            : "Institution attendance."
        }
        action={
          <>
            <DepartmentFilter value={department} onChange={setDepartment} />
            <AskAgentLink to="/admin/agents/academic" label="Ask Academic Agent" />
          </>
        }
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
              { id: "departments", label: "By department", count: data.by_department.length },
              { id: "courses", label: "By course", count: data.insights.courses.length },
              { id: "sections", label: "By section", count: data.insights.sections.length },
              { id: "live", label: "Live sessions", count: data.active_sessions.length },
            ]}
          />
          <TabPanel>
            <Card className="overflow-hidden">
              {tab === "departments" && (
                <DataTable columns={["Department", "Students", "Attended", "Attendance", "Below requirement"]} caption="Attendance by department">
                  {data.by_department.map((d) => (
                    <tr key={d.code}>
                      <td>
                        <CellTitle title={d.code} subtitle={d.name} />
                      </td>
                      <td className="tabular-nums">{d.students}</td>
                      <td className="tabular-nums">
                        {d.classes_attended}/{d.classes_conducted}
                      </td>
                      <td className="tabular-nums">{pct(d.percentage)}</td>
                      <td>{d.students_below_threshold ? <StatusBadge label={String(d.students_below_threshold)} tone="danger" /> : <span className="text-muted">0</span>}</td>
                    </tr>
                  ))}
                </DataTable>
              )}
              {tab === "courses" && (
                <DataTable columns={["Course", "Section", "Faculty", "Attendance", "Below"]} caption="Attendance by course">
                  {data.insights.courses.map((c) => (
                    <tr key={c.assignment_id}>
                      <td>
                        <CellTitle title={c.course_title} subtitle={c.course_code} />
                      </td>
                      <td>{c.class_label}</td>
                      <td>{c.faculty_name}</td>
                      <td className="tabular-nums">{pct(c.percentage)}</td>
                      <td>{c.below_threshold ? <StatusBadge label={String(c.below_threshold)} tone="danger" /> : <span className="text-muted">0</span>}</td>
                    </tr>
                  ))}
                </DataTable>
              )}
              {tab === "sections" && (
                <DataTable columns={["Section", "Students", "Attendance", "Below"]} caption="Attendance by section">
                  {data.insights.sections.map((s) => (
                    <tr key={s.class_label}>
                      <td className="font-medium">{s.class_label}</td>
                      <td className="tabular-nums">{s.students}</td>
                      <td className="tabular-nums">{pct(s.percentage)}</td>
                      <td>{s.below_threshold ? <StatusBadge label={String(s.below_threshold)} tone="danger" /> : <span className="text-muted">0</span>}</td>
                    </tr>
                  ))}
                </DataTable>
              )}
              {tab === "live" &&
                (data.active_sessions.length === 0 ? (
                  <EmptyState icon={<Radio />} title="No class is in session right now" description="Live sessions appear here with how many students are marked." />
                ) : (
                  <DataTable columns={["Class", "Started", "Present", "Not marked"]} caption="Live sessions">
                    {data.active_sessions.map((s) => (
                      <tr key={s.session_id}>
                        <td>
                          <CellTitle title={s.course_title} subtitle={`${s.class_label} · ${s.faculty_name}`} />
                        </td>
                        <td className="text-sm">{s.actual_started_at ? formatDateTime(s.actual_started_at) : "—"}</td>
                        <td className="tabular-nums">
                          {s.tally.present + s.tally.late}/{s.tally.roster}
                        </td>
                        <td>{s.tally.unmarked ? <StatusBadge label={String(s.tally.unmarked)} tone="warning" /> : <span className="text-muted">0</span>}</td>
                      </tr>
                    ))}
                  </DataTable>
                ))}
            </Card>
          </TabPanel>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Requests, complaints
// ---------------------------------------------------------------------------

export function AdminRequestsPage() {
  return (
    <div className="space-y-6">
      <PageTitle
        title="Requests"
        description="Requests routed to the administration: HOD requests and anything no one below could take. Routing history is in each request."
        action={<AskAgentLink to="/admin/agents/permission" label="Ask Permission Agent" />}
      />
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
  const pastSla = data?.filter((c) => (c.response_breached || c.resolution_breached) && (c.status === "open" || c.status === "in_progress")).length ?? 0;
  return (
    <div className="space-y-6">
      <PageTitle
        title="Complaints"
        description="Institution-wide complaints and their SLA state. Read-only; cases change only through the controlled complaint workflow."
        action={<AskAgentLink to="/admin/agents/complaints" label="Ask Complaints Agent" />}
      />
      <Toolbar>
        <DepartmentFilter value={department} onChange={setDepartment} />
        <Select aria-label="Status filter" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">Any status</option>
          <option value="open">Open</option>
          <option value="in_progress">In progress</option>
          <option value="resolved">Resolved</option>
          <option value="closed">Closed</option>
        </Select>
        <label className="inline-flex items-center gap-2 text-sm text-ink">
          <input type="checkbox" checked={breached} onChange={(e) => setBreached(e.target.checked)} className="size-4 accent-accent" /> SLA breached only
        </label>
        {data && (
          <span className="text-sm text-muted sm:ml-auto">
            {data.length} complaints · {pastSla} open past SLA
          </span>
        )}
      </Toolbar>
      {isLoading && <SkeletonTable rows={5} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <Card className="overflow-hidden">
          {data.length === 0 ? (
            <EmptyState icon={<ShieldAlert />} title="No complaints match" description="Try a different department, status or filter." />
          ) : (
            <DataTable columns={["Case", "Department", "Priority", "Status", "SLA"]} caption="Complaints">
              {data.map((c) => {
                const breach = c.response_breached || c.resolution_breached;
                return (
                  <tr key={c.case_code}>
                    <td>
                      <CellTitle title={c.case_code} subtitle={`${titleCase(c.category)} · ${c.office}`} />
                    </td>
                    <td>{c.student_department}</td>
                    <td>{titleCase(c.priority)}</td>
                    <td>
                      <StatusBadge label={titleCase(c.status)} tone={c.status === "resolved" || c.status === "closed" ? "neutral" : "warning"} />
                    </td>
                    <td>
                      {breach ? (
                        <StatusBadge label={c.resolution_breached ? "Resolution breached" : "Response breached"} tone="danger" />
                      ) : (
                        <span className="text-sm text-muted">{c.resolution_due_at ? `Due ${formatDateTime(c.resolution_due_at)}` : "—"}</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </DataTable>
          )}
        </Card>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// AI operations, audit log
// ---------------------------------------------------------------------------

function Panel({ title, action, children }: { title: string; action?: ReactNode; children: ReactNode }) {
  return (
    <Card className="overflow-hidden">
      <CardHeader title={title} action={action} />
      <div className="border-t border-border">{children}</div>
    </Card>
  );
}

export function AdminAIOperationsPage() {
  const { data, isLoading, isError, error, refetch } = useAdminAIOperations();
  const missions = data ? Object.values(data.missions_by_status).reduce((a, b) => a + b, 0) : 0;
  return (
    <div className="space-y-6">
      <PageTitle
        title="AI Operations"
        description="Routing, usage and cost per intelligence level, the Control Tower of recent missions, readiness and recent issues. Costs are estimates; missing token counts are shown as unavailable."
        action={<AskAgentLink to="/admin/agents/enquiry" label="Ask CampusNexus about operations" question="Are there any failed agent workflows today?" />}
      />
      {isLoading && <SkeletonTable rows={6} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <>
          {!data.live && <Notice tone="warning">LLM provider is the offline mock ({data.provider}). Answers are deterministic, not live AI.</Notice>}
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <Panel title="Provider">
              <div className="flex items-center justify-between gap-4 px-5 py-4">
                <div className="min-w-0">
                  <p className="text-lg font-semibold text-ink">{titleCase(data.provider)}</p>
                  <p className="truncate font-mono text-[13px] text-muted">{data.model ?? "no model configured"}</p>
                </div>
                <StatusBadge label={data.live ? "Live" : "Deterministic"} tone={data.live ? "live" : "neutral"} pulse={data.live} />
              </div>
            </Panel>
            <Panel title="System readiness">
              <ul className="divide-y divide-border">
                {[
                  { name: "Database", ok: data.database_ready, detail: "" },
                  { name: "RAG / policy knowledge", ok: data.rag_ready, detail: `${data.rag_chunks} chunks` },
                ].map((c) => (
                  <li key={c.name} className="flex items-center justify-between gap-3 px-5 py-3">
                    <span className="text-sm text-ink">
                      {c.name}
                      {c.detail && <span className="ml-2 text-xs text-muted">{c.detail}</span>}
                    </span>
                    <StatusBadge label={c.ok ? "Ready" : "Unavailable"} tone={c.ok ? "success" : "danger"} />
                  </li>
                ))}
              </ul>
            </Panel>
          </div>
          <IntelligencePanel usage={data.usage} routing={data.routing} />
          <AgentRuns rows={data.agent_runs} />
          <ControlTower rows={data.control_tower} />
          <Panel title="Agent activity">
            <dl className="grid grid-cols-2 divide-border sm:grid-cols-4 sm:divide-x">
              {[
                { label: "Missions", value: missions, note: `${data.failed_missions} failed` },
                { label: "Agent runs", value: data.agent_runs_total, note: `${data.agent_runs_failed} failed` },
                { label: "Failures", value: data.workflow_failures, note: `${data.requests_needing_review} requests need review` },
                { label: "Pending approvals", value: data.pending_approvals, note: `${data.stale_approvals} stale` },
              ].map((m) => (
                <div key={m.label} className="px-5 py-4">
                  <dt className="text-xs font-medium text-muted">{m.label}</dt>
                  <dd className="mt-1 text-2xl font-semibold tabular-nums text-ink">{m.value}</dd>
                  <dd className="text-xs text-muted">{m.note}</dd>
                </div>
              ))}
            </dl>
          </Panel>
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <Panel title="Recent issues">
              <ul className="divide-y divide-border">
                <li className="flex items-center justify-between gap-3 px-5 py-3">
                  <span className="text-sm text-ink">Provider errors</span>
                  <span className="text-right">
                    <span className={cn("block text-sm font-semibold tabular-nums", data.provider_errors ? "text-danger-strong" : "text-muted")}>{data.provider_errors}</span>
                    <span className="block text-xs text-muted">{data.rate_limit_incidents} rate-limit incidents</span>
                  </span>
                </li>
                <li className="flex items-center justify-between gap-3 px-5 py-3">
                  <span className="text-sm text-ink">Workflow failures</span>
                  <span className={cn("text-sm font-semibold tabular-nums", data.workflow_failures ? "text-danger-strong" : "text-muted")}>{data.workflow_failures}</span>
                </li>
                {[...data.recent_provider_errors, ...data.recent_workflow_failures].slice(0, 6).map((e, i) => (
                  <li key={`${e.reference}-${i}`} className="px-5 py-3">
                    <div className="flex items-baseline justify-between gap-3">
                      <span className="text-sm font-medium text-ink">{titleCase(e.event_type)}</span>
                      <span className="shrink-0 text-xs text-subtle">{formatDateTime(e.timestamp)}</span>
                    </div>
                    <p className="mt-0.5 line-clamp-2 text-[13px] text-muted">{e.message}</p>
                  </li>
                ))}
              </ul>
            </Panel>
            <Panel title="Recent missions">
              {data.recent_missions.length === 0 ? (
                <EmptyState compact title="No missions yet" />
              ) : (
                <ul className="divide-y divide-border">
                  {data.recent_missions.slice(0, 6).map((m) => (
                    <li key={m.mission_id} className="flex items-start justify-between gap-3 px-5 py-3">
                      <div className="min-w-0">
                        <p className="line-clamp-2 text-sm text-ink">{m.goal}</p>
                        <p className="text-xs text-muted">{formatDateTime(m.updated_at)}</p>
                      </div>
                      <StatusBadge {...MISSION_STATUS(m.status)} />
                    </li>
                  ))}
                </ul>
              )}
            </Panel>
          </div>
          <p className="text-xs text-muted">Token usage: {data.token_usage}</p>
        </>
      )}
    </div>
  );
}

export function AdminAgentCatalogPage() {
  const { data, isLoading, isError, error, refetch } = useAdminAgentCatalog();
  const entries = useAdminAgentsCatalog();
  const deployments = useAdminDeployments();
  return (
    <div className="space-y-6">
      <PageTitle
        title="Agent Catalog"
        description="Configure and deploy product agents for this institution: status, intelligence level, budget and where a human must approve."
      />
      <LifecycleStrip />
      {(isLoading || entries.isLoading) && <SkeletonTable rows={4} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {deployments.data && <DeployedAgents rows={deployments.data} />}
      {entries.data && <DeployableCatalog entries={entries.data} />}
      {data && <AgentCatalog data={data} />}
    </div>
  );
}

export function AdminAuditPage() {
  const [source, setSource] = useState("");
  const [action, setAction] = useState("");
  const [open, setOpen] = useState<AuditEntry | null>(null);
  const { data, isLoading, isError, error, refetch } = useAdminAudit({ source: source || undefined, action: action || undefined });
  return (
    <div className="space-y-6">
      <PageTitle title="Audit Log" description="Append-only mission and operations audit trails, newest first. Read-only." />
      <Toolbar>
        <Select aria-label="Source filter" value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="">All sources</option>
          <option value="operations">Operations</option>
          <option value="mission">Missions & agents</option>
        </Select>
        <SearchInput aria-label="Action filter" value={action} onChange={(e) => setAction(e.target.value)} placeholder="Action contains… e.g. request, class, login" />
        {data && <span className="text-sm text-muted sm:ml-auto">{data.length} entries</span>}
      </Toolbar>
      {isLoading && <SkeletonTable rows={8} />}
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {data && (
        <Card className="overflow-hidden">
          {data.length === 0 ? (
            <EmptyState icon={<ScrollText />} title="No audit entries match" description="Try a different source or action." />
          ) : (
            <DataTable columns={["Time", "Actor", "Role", "Action", "Resource", "Result"]} caption="Audit log">
              {data.map((e, i) => (
                <tr key={`${e.timestamp}-${i}`} data-clickable="" tabIndex={0} onClick={() => setOpen(e)} onKeyDown={rowKeys(() => setOpen(e))}>
                  <td className="text-[13px] whitespace-nowrap text-muted tabular-nums">{formatDateTime(e.timestamp)}</td>
                  <td className="font-medium">{e.actor}</td>
                  <td className="text-muted">{roleLabel(e.role)}</td>
                  <td>
                    <Badge tone={e.source === "mission" ? "info" : "neutral"}>{e.action.replace(/_/g, " ")}</Badge>
                  </td>
                  <td className="text-muted">{titleCase(e.target)}</td>
                  <td className="max-w-md">
                    <p className="line-clamp-2 text-[13px] text-ink">{e.outcome}</p>
                  </td>
                </tr>
              ))}
            </DataTable>
          )}
        </Card>
      )}
      <Sheet open={open !== null} onClose={() => setOpen(null)} title="Audit event" description={open ? formatDateTime(open.timestamp) : undefined}>
        {open && (
          <div className="px-5 py-5">
            <DetailList>
              <DetailRow label="Action">{open.action.replace(/_/g, " ")}</DetailRow>
              <DetailRow label="Actor">{open.actor}</DetailRow>
              <DetailRow label="Role">{roleLabel(open.role)}</DetailRow>
              <DetailRow label="Source">{open.source === "mission" ? "Missions & agents" : "Operations"}</DetailRow>
              <DetailRow label="Resource">{titleCase(open.target)}</DetailRow>
              <DetailRow label="Reference">
                <code className="font-mono text-[13px] break-all">{open.reference}</code>
              </DetailRow>
              <DetailRow label="Timestamp">
                <code className="font-mono text-[13px]">{open.timestamp}</code>
              </DetailRow>
            </DetailList>
            <h3 className="mt-6 text-xs font-semibold tracking-wide text-muted uppercase">Result</h3>
            <p className="mt-2 text-sm text-ink">{open.outcome}</p>
          </div>
        )}
      </Sheet>
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
      <AgentDirectory agents={ADMIN_AGENTS} basePath="/admin/agents" />
    </div>
  );
}

export function AdminAgentPage() {
  return <RoleAgentPage scope="admin" resolve={adminAgentByKey} basePath="/admin/agents" />;
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
    <div className="max-w-4xl space-y-6">
      <PageTitle title="Settings" description="Your account and a safe summary of how this instance is configured. Secrets are never shown — only whether they are set." />
      <div>
        {user && (
          <FormSection title="Account" description="The administrator signed in on this device.">
            <DetailList>
              <DetailRow label="Name">{user.display_name}</DetailRow>
              <DetailRow label="Email">{user.email}</DetailRow>
              <DetailRow label="Role">Administrator</DetailRow>
            </DetailList>
          </FormSection>
        )}
        {isLoading && <SkeletonTable rows={5} />}
        {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
        {data && (
          <>
            <FormSection title="Workspace" description={data.overall === "ready" ? "All components are ready." : "Some components are degraded."}>
              <ul className="divide-y divide-border">
                {data.components.map((c) => (
                  <li key={c.name} className="flex items-start justify-between gap-3 py-3">
                    <CellTitle title={c.name} subtitle={c.detail} />
                    <StatusBadge {...statusOf(COMPONENT_HEALTH, c.status)} />
                  </li>
                ))}
              </ul>
            </FormSection>
            <FormSection title="System information" description="Read-only configuration summary.">
              <DetailList>
                {Object.entries(data.settings).map(([key, value]) => (
                  <DetailRow key={key} label={SETTING_LABELS[key] ?? titleCase(key)}>
                    {settingValue(value)}
                  </DetailRow>
                ))}
              </DetailList>
            </FormSection>
          </>
        )}
      </div>
    </div>
  );
}
