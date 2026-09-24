import { CalendarDays, Clock, UserRound } from "lucide-react";
import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { MARK, STANDING, WORKFLOW_STATUS } from "@/components/dashboard/status";
import type { WorkflowRequest } from "@/types/api";
import { formatDate, formatDateTime, formatTime, titleCase } from "@/utils/format";

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] font-medium uppercase tracking-wide text-subtle">{label}</dt>
      <dd className="mt-0.5 text-sm">{children}</dd>
    </div>
  );
}

const DAY_PART: Record<string, string> = { full_day: "Full day", morning: "Morning", afternoon: "Afternoon" };

/** The structured context collected for a request: event, day, affected classes and attendance. */
function FacultyImpact({ request }: { request: WorkflowRequest }) {
  const ctx = request.context;
  return (
    <div className="space-y-3">
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <Field label="Faculty">
          {ctx.faculty_name}
          <div className="text-xs text-muted">
            {ctx.faculty_designation} · {ctx.faculty_employee_code} · {ctx.department_code}
          </div>
        </Field>
        {ctx.event ? (
          <Field label="Event">
            {ctx.event.title}
            <div className="text-xs text-muted">
              {formatDate(ctx.event.starts_at)}, {formatTime(ctx.event.starts_at)}–{formatTime(ctx.event.ends_at)}
            </div>
          </Field>
        ) : (
          ctx.request_date && (
            <Field label="Date">
              {formatDate(`${ctx.request_date}T12:00:00+05:30`)}
              {ctx.day_part && <div className="text-xs text-muted">{DAY_PART[ctx.day_part]}</div>}
            </Field>
          )
        )}
        <Field label="Classes affected">{ctx.affected_classes.length || "None"}</Field>
      </dl>
      {ctx.affected_classes.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-border bg-surface-muted text-xs text-muted">
                <th className="px-3 py-2 font-medium">Affected class</th>
                <th className="px-3 py-2 font-medium">Group</th>
                <th className="px-3 py-2 font-medium">Time</th>
                <th className="px-3 py-2 font-medium">Students</th>
                <th className="px-3 py-2 font-medium">Substitute</th>
              </tr>
            </thead>
            <tbody>
              {ctx.affected_classes.map((c) => (
                <tr key={`${c.course_code}-${c.starts_at}`} className="border-b border-border last:border-0">
                  <td className="px-3 py-2">
                    {c.course_title}
                    <div className="text-xs text-muted">{c.course_code} · {c.room}</div>
                  </td>
                  <td className="px-3 py-2">{c.class_label}</td>
                  <td className="px-3 py-2 tabular-nums">
                    {formatDate(c.starts_at)}
                    <div className="text-xs text-muted">{c.start_local}–{c.end_local}</div>
                  </td>
                  <td className="px-3 py-2 tabular-nums">{c.roster_size ?? "—"}</td>
                  <td className="px-3 py-2 text-muted">{c.substitute ?? "Not assigned"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {ctx.notes.map((note) => (
        <p key={note} className="text-xs text-muted">
          {note}
        </p>
      ))}
    </div>
  );
}

export function RequestContextView({ request }: { request: WorkflowRequest }) {
  const ctx = request.context;
  if (ctx.requester_kind === "faculty") return <FacultyImpact request={request} />;
  return (
    <div className="space-y-3">
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        {ctx.event && (
          <Field label="Event">
            {ctx.event.title}
            <div className="text-xs text-muted">
              {formatDate(ctx.event.starts_at)}, {formatTime(ctx.event.starts_at)}–{formatTime(ctx.event.ends_at)} · {ctx.event.location}
            </div>
          </Field>
        )}
        {!ctx.event && ctx.request_date && (
          <Field label="Date">
            {formatDate(`${ctx.request_date}T12:00:00+05:30`)}
            {ctx.day_part && <div className="text-xs text-muted">{DAY_PART[ctx.day_part]}</div>}
          </Field>
        )}
        {ctx.student_name && (
          <Field label="Student">
            {ctx.student_name}
            <div className="text-xs text-muted">
              {request.student_id} · {ctx.department_code} Year {ctx.student_year}, Section {ctx.student_section}
            </div>
          </Field>
        )}
        <Field label="Timetable conflict">{ctx.timetable_conflict ? `Yes — ${ctx.affected_classes.length} class${ctx.affected_classes.length === 1 ? "" : "es"}` : "None"}</Field>
      </dl>
      {ctx.affected_classes.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-border bg-surface-muted text-xs text-muted">
                <th className="px-3 py-2 font-medium">Affected class</th>
                <th className="px-3 py-2 font-medium">Time</th>
                <th className="px-3 py-2 font-medium">Faculty</th>
                <th className="px-3 py-2 font-medium">Attendance</th>
                <th className="px-3 py-2 font-medium">Marked</th>
              </tr>
            </thead>
            <tbody>
              {ctx.affected_classes.map((c) => (
                <tr key={`${c.course_code}-${c.starts_at}`} className="border-b border-border last:border-0">
                  <td className="px-3 py-2">
                    {c.course_title}
                    <div className="text-xs text-muted">{c.course_code} · {c.room}</div>
                  </td>
                  <td className="px-3 py-2 tabular-nums">
                    {formatDate(c.starts_at)}
                    <div className="text-xs text-muted">{c.start_local}–{c.end_local}</div>
                  </td>
                  <td className="px-3 py-2">{c.faculty_name}</td>
                  <td className="px-3 py-2">
                    {c.attendance_percentage != null ? `${c.attendance_percentage.toFixed(1)}%` : "—"}
                    {c.standing !== "unknown" && (
                      <Badge tone={STANDING[c.standing].tone} className="ml-2">
                        {STANDING[c.standing].label}
                      </Badge>
                    )}
                  </td>
                  <td className="px-3 py-2">{c.my_mark ? <Badge tone={MARK[c.my_mark].tone}>{MARK[c.my_mark].label}</Badge> : <span className="text-muted">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {ctx.notes.map((note) => (
        <p key={note} className="text-xs text-muted">
          {note}
        </p>
      ))}
    </div>
  );
}

/** One permission / leave / OD request, as its student or its reviewer sees it. */
export function WorkflowRequestCard({ request, viewer, actions }: { request: WorkflowRequest; viewer: "student" | "faculty"; actions?: ReactNode }) {
  const status = WORKFLOW_STATUS[request.status] ?? { label: request.status, tone: "neutral" as const };
  const decided = request.status === "approved" || request.status === "rejected";
  return (
    <Card className="space-y-4 px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone="primary">{request.type_label}</Badge>
            <span className="text-xs text-subtle">{request.request_id}</span>
          </div>
          <h3 className="mt-1.5 text-base font-semibold">{viewer === "faculty" && request.requester_name ? `${request.requester_name} — ${request.title}` : request.title}</h3>
          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
            <span className="inline-flex items-center gap-1">
              <Clock className="size-3.5" /> Submitted {formatDateTime(request.submitted_at ?? request.created_at)}
            </span>
            {request.routing_basis === "hod_escalation" && <Badge tone="warning">Escalated to HOD</Badge>}
            {viewer === "student" && (
              <span className="inline-flex items-center gap-1">
                <UserRound className="size-3.5" /> {request.reviewer_name ? `Reviewer: ${request.reviewer_name}` : "No reviewer assigned yet"}
              </span>
            )}
            {request.context.event && (
              <span className="inline-flex items-center gap-1">
                <CalendarDays className="size-3.5" /> {request.context.event.title}
              </span>
            )}
          </div>
        </div>
        <Badge tone={status.tone}>{status.label}</Badge>
      </div>

      <div>
        <p className="text-[11px] font-medium uppercase tracking-wide text-subtle">Reason</p>
        <p className="mt-0.5 text-sm">{request.reason}</p>
      </div>

      <RequestContextView request={request} />

      {request.status === "needs_review" && <p className="text-xs text-warning">{request.routing_note}</p>}
      {viewer === "faculty" && (request.routing_history?.length ?? 0) > 0 && (
        <details className="rounded-lg border border-border px-3 py-2">
          <summary className="cursor-pointer text-xs font-medium text-muted">
            Routing history ({request.routing_history!.length}) · {request.requester_role ? `${titleCase(request.requester_role)} · ` : ""}
            {request.department_code ?? ""} · current reviewer: {request.reviewer_name ?? "none"}
          </summary>
          <ol className="mt-2 space-y-1">
            {request.routing_history!.map((h, i) => (
              <li key={i} className="text-xs">
                <span className="text-subtle">{formatDateTime(h.at)}</span> · <span className="font-medium">{titleCase(h.basis)}</span> → {h.reviewer}: {h.note}
              </li>
            ))}
          </ol>
        </details>
      )}
      {decided && (
        <div className={request.status === "approved" ? "rounded-lg border border-success/20 bg-success-soft px-4 py-3" : "rounded-lg border border-danger/20 bg-danger-soft px-4 py-3"}>
          <p className="text-sm font-medium">
            {request.status === "approved" ? "Approved" : "Rejected"} by {request.decided_by} · {formatDateTime(request.decided_at)}
          </p>
          {request.decision_reason && <p className="mt-0.5 text-sm">“{request.decision_reason}”</p>}
        </div>
      )}
      {actions}
    </Card>
  );
}
