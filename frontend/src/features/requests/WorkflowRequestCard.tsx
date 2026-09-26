import { useState } from "react";
import type { ReactNode } from "react";
import { Badge, StatusBadge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { CellTitle, DataTable } from "@/components/ui/data-table";
import { DetailList, DetailRow } from "@/components/ui/detail-list";
import { Stepper } from "@/components/ui/stepper";
import { requestHeading, requestProgress } from "@/features/requests/requestStatus";
import { MARK, STANDING, WORKFLOW_STATUS, statusOf } from "@/components/dashboard/status";
import type { WorkflowRequest } from "@/types/api";
import { formatDate, formatDateTime, formatTime, titleCase } from "@/utils/format";

const DAY_PART: Record<string, string> = { full_day: "Full day", morning: "Morning", afternoon: "Afternoon" };

function SectionTitle({ children }: { children: ReactNode }) {
  return <h3 className="text-xs font-semibold tracking-wide text-muted uppercase">{children}</h3>;
}

const requestDate = (request: WorkflowRequest) => {
  const ctx = request.context;
  if (!ctx.request_date) return null;
  return (
    <>
      {formatDate(`${ctx.request_date}T12:00:00+05:30`)}
      {ctx.day_part && <div className="text-xs font-normal text-muted">{DAY_PART[ctx.day_part]}</div>}
    </>
  );
};

/** A faculty member's request: who, when, and the classes it would affect (substitute is never invented). */
function FacultyImpact({ request }: { request: WorkflowRequest }) {
  const ctx = request.context;
  const date = requestDate(request);
  return (
    <div className="space-y-4">
      <DetailList>
        <DetailRow label="Faculty">
          {ctx.faculty_name}
          <div className="text-xs font-normal text-muted">
            {ctx.faculty_designation} · {ctx.department_code}
          </div>
        </DetailRow>
        {ctx.event ? (
          <DetailRow label="Event">
            {ctx.event.title}
            <div className="text-xs font-normal text-muted">
              {formatDate(ctx.event.starts_at)}, {formatTime(ctx.event.starts_at)}–{formatTime(ctx.event.ends_at)}
            </div>
          </DetailRow>
        ) : (
          date && <DetailRow label="Date">{date}</DetailRow>
        )}
        <DetailRow label="Classes affected">{ctx.affected_classes.length || "None"}</DetailRow>
      </DetailList>
      {ctx.affected_classes.length > 0 && (
        <div className="overflow-hidden rounded-md border border-border">
          <DataTable columns={["Affected class", "Group", "Students", "Substitute"]} caption="Classes affected by this request" flush>
            {ctx.affected_classes.map((c) => (
              <tr key={`${c.course_code}-${c.starts_at}`}>
                <td>
                  <CellTitle title={c.course_title} subtitle={`${formatDate(c.starts_at)} · ${c.start_local}–${c.end_local} · ${c.room}`} />
                </td>
                <td className="whitespace-nowrap">{c.class_label}</td>
                <td className="tabular-nums">{c.roster_size ?? "—"}</td>
                <td className="text-muted">{c.substitute ?? "Not assigned"}</td>
              </tr>
            ))}
          </DataTable>
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

/** The structured context collected for a request: event, day, affected classes and attendance. */
export function RequestContextView({ request }: { request: WorkflowRequest }) {
  const ctx = request.context;
  if (ctx.requester_kind === "faculty") return <FacultyImpact request={request} />;
  const date = requestDate(request);
  return (
    <div className="space-y-4">
      <DetailList>
        {ctx.event && (
          <DetailRow label="Event">
            {ctx.event.title}
            <div className="text-xs font-normal text-muted">
              {formatDate(ctx.event.starts_at)}, {formatTime(ctx.event.starts_at)}–{formatTime(ctx.event.ends_at)} · {ctx.event.location}
            </div>
          </DetailRow>
        )}
        {!ctx.event && date && <DetailRow label="Date">{date}</DetailRow>}
        {ctx.student_name && (
          <DetailRow label="Student">
            {ctx.student_name}
            <div className="text-xs font-normal text-muted">
              {ctx.department_code} Year {ctx.student_year}, Section {ctx.student_section}
            </div>
          </DetailRow>
        )}
        <DetailRow label="Timetable conflict">
          {ctx.timetable_conflict ? `Yes — ${ctx.affected_classes.length} class${ctx.affected_classes.length === 1 ? "" : "es"}` : "None"}
        </DetailRow>
      </DetailList>
      {ctx.affected_classes.length > 0 && (
        <div className="overflow-hidden rounded-md border border-border">
          <DataTable columns={["Affected class", "Attendance", "Marked"]} caption="Classes affected by this request" flush>
            {ctx.affected_classes.map((c) => (
              <tr key={`${c.course_code}-${c.starts_at}`}>
                <td>
                  <CellTitle title={c.course_title} subtitle={`${formatDate(c.starts_at)} · ${c.start_local}–${c.end_local} · ${c.faculty_name}`} />
                </td>
                <td>
                  <span className="inline-flex flex-wrap items-center gap-2">
                    <span className="tabular-nums">{c.attendance_percentage != null ? `${c.attendance_percentage.toFixed(1)}%` : "—"}</span>
                    {c.standing !== "unknown" && <StatusBadge {...STANDING[c.standing]} />}
                  </span>
                </td>
                <td>{c.my_mark ? <StatusBadge {...MARK[c.my_mark]} /> : <span className="text-muted">—</span>}</td>
              </tr>
            ))}
          </DataTable>
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

/** Full request detail for drawers: progress, request information, context and decision history. */
export function RequestDetail({ request, viewer, showTitle = false }: { request: WorkflowRequest; viewer: "student" | "faculty"; showTitle?: boolean }) {
  const requester = viewer === "student";
  const history = request.routing_history ?? [];
  const decided = request.status === "approved" || request.status === "rejected";
  return (
    <div className="space-y-6">
      {showTitle && (
        <div>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <Badge tone="primary">{request.type_label}</Badge>
            <span className="flex items-center gap-1.5">
              <SlaChip request={request} />
              <StatusBadge {...statusOf(WORKFLOW_STATUS, request.status)} />
            </span>
          </div>
          <h3 className="mt-2 text-base font-semibold text-ink">{requestHeading(request, viewer)}</h3>
        </div>
      )}
      <Stepper orientation="horizontal" steps={requestProgress(request)} />

      <section>
        <SectionTitle>Request information</SectionTitle>
        <DetailList className="mt-1">
          {!requester && request.requester_name && (
            <DetailRow label="Requested by">
              {request.requester_name}
              {request.department_code && <div className="text-xs font-normal text-muted">{request.department_code}</div>}
            </DetailRow>
          )}
          <DetailRow label="Reason">
            <span className="font-normal">{request.reason}</span>
          </DetailRow>
          <DetailRow label={request.submitted_at ? "Submitted" : "Prepared"}>{formatDateTime(request.submitted_at ?? request.created_at)}</DetailRow>
          <DetailRow label="Reviewer">{request.reviewer_name ?? "Not assigned yet"}</DetailRow>
          {request.routing_basis === "hod_escalation" && <DetailRow label="Routing">Escalated to HOD</DetailRow>}
        </DetailList>
        {request.status === "needs_review" && <p className="mt-2 text-sm text-caution-strong">{request.routing_note}</p>}
      </section>

      <section>
        <SectionTitle>Context</SectionTitle>
        <div className="mt-1">
          <RequestContextView request={request} />
        </div>
      </section>

      {(decided || history.length > 0) && (
        <section>
          <SectionTitle>{history.length > 0 ? `Routing history (${history.length})` : "Decision"}</SectionTitle>
          <ol className="mt-3 space-y-3 border-l border-border pl-4">
            {history.map((h, i) => (
              <li key={i} className="text-sm text-ink">
                <span className="font-medium">{titleCase(h.basis)}</span> → {h.reviewer}
                <div className="text-xs text-muted">
                  {formatDateTime(h.at)} · {h.note}
                </div>
              </li>
            ))}
            {decided && (
              <li className="text-sm text-ink">
                <span className="font-medium">{request.status === "approved" ? "Approved" : "Rejected"}</span> by {request.decided_by}
                <div className="text-xs text-muted">{formatDateTime(request.decided_at)}</div>
                {request.decision_reason && <p className="mt-1 text-sm">“{request.decision_reason}”</p>}
              </li>
            )}
          </ol>
        </section>
      )}
    </div>
  );
}

/** A request as a standalone card (request detail with its own heading). */
/** Demo SLA indicator for an open request (48 h review target, "due soon" after 24 h). Computed on read;
 * routing escalation is real, a scheduled auto-escalation job is on the roadmap. */
export function SlaChip({ request }: { request: WorkflowRequest }) {
  const [now] = useState(() => Date.now()); // captured once per mount: render stays pure
  if (request.status !== "pending" && request.status !== "needs_review") return null;
  const escalated = request.status === "needs_review" || /escalation/.test(request.routing_basis ?? "");
  const hours = (now - new Date(request.submitted_at ?? request.created_at).getTime()) / 3_600_000;
  const [label, tone] = escalated
    ? (["Escalated", "danger"] as const)
    : hours >= 48
      ? (["SLA Risk", "danger"] as const)
      : hours >= 24
        ? (["Due Soon", "warning"] as const)
        : (["On Track", "success"] as const);
  return <StatusBadge label={label} tone={tone} title={`Demo SLA indicator · ${Math.max(0, Math.round(hours))} h elapsed of a 48 h review target`} />;
}

export function WorkflowRequestCard({ request, viewer, actions }: { request: WorkflowRequest; viewer: "student" | "faculty"; actions?: ReactNode }) {
  return (
    <Card className="overflow-hidden">
      <div className="p-5">
        <RequestDetail request={request} viewer={viewer} showTitle />
      </div>
      {actions && <div className="border-t border-border px-5 py-4">{actions}</div>}
    </Card>
  );
}
