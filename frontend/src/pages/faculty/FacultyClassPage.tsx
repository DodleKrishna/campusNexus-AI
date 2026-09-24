import { ArrowLeft, CheckCheck, Loader2, Play, Square, XCircle } from "lucide-react";
import { Link, Navigate, useParams } from "react-router-dom";
import { ApiError } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { SkeletonRows } from "@/components/ui/skeleton";
import { ErrorState, Notice } from "@/components/ui/states";
import { MARK, SESSION, STANDING } from "@/components/dashboard/status";
import { useClassAction, useFacultyClass } from "@/hooks/useFacultyData";
import type { FacultyClassDetail, MarkStatus, RosterEntry } from "@/types/api";
import { cn } from "@/utils/cn";
import { formatDate, formatTime } from "@/utils/format";

const MARK_OPTIONS: MarkStatus[] = ["present", "absent", "late", "excused"];
const MARK_STYLE: Record<MarkStatus, string> = {
  present: "border-success bg-success-soft text-success",
  absent: "border-danger-strong bg-danger-soft text-danger-strong",
  late: "border-warning bg-warning-soft text-warning",
  excused: "border-info bg-info-soft text-info",
};

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-[11px] font-medium uppercase tracking-wide text-subtle">{label}</dt>
      <dd className="mt-0.5 text-sm font-medium">{value}</dd>
    </div>
  );
}

function MarkControl({ entry, editable, disabled, onMark }: { entry: RosterEntry; editable: boolean; disabled: boolean; onMark: (status: MarkStatus) => void }) {
  if (!editable) {
    return entry.mark ? <Badge tone={MARK[entry.mark].tone}>{MARK[entry.mark].label}</Badge> : <span className="text-xs text-muted">Not marked</span>;
  }
  return (
    <div role="radiogroup" aria-label={`Attendance for ${entry.full_name}`} className="flex flex-wrap gap-1">
      {MARK_OPTIONS.map((status) => (
        <button
          key={status}
          type="button"
          role="radio"
          aria-checked={entry.mark === status}
          disabled={disabled}
          onClick={() => entry.mark !== status && onMark(status)}
          className={cn(
            "rounded-md border px-2.5 py-1 text-xs font-medium transition-colors disabled:opacity-60",
            entry.mark === status ? MARK_STYLE[status] : "border-border text-muted hover:border-border-strong hover:text-ink",
          )}
        >
          {MARK[status].label}
        </button>
      ))}
    </div>
  );
}

function Workspace({ detail, sessionId }: { detail: FacultyClassDetail; sessionId: number }) {
  const action = useClassAction(sessionId);
  const info = detail.class_info;
  const { tally } = info;
  const status = SESSION[info.status];
  const editable = info.status === "active";
  const busy = action.isPending;
  const error = action.error instanceof ApiError ? action.error.message : action.error ? "That didn't work. Please try again." : null;

  return (
    <div className="space-y-6">
      <Link to="/faculty/classes" className="inline-flex items-center gap-1 text-sm text-muted hover:text-ink">
        <ArrowLeft className="size-4" /> My classes
      </Link>

      <Card className="px-5 py-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={status.tone}>
                {info.status === "active" && <span className="size-1.5 animate-pulse rounded-full bg-current" />}
                {status.label}
              </Badge>
              {info.is_extra_class && <Badge tone="neutral">Extra class</Badge>}
            </div>
            <h1 className="mt-2 text-2xl font-semibold tracking-tight">{info.course_title}</h1>
            <p className="mt-1 text-sm text-muted">
              {info.course_code} · {info.department_code} Year {info.year}, Semester {info.semester}, Section {info.section}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            {info.status === "scheduled" && (
              <>
                <Button variant="ghost" onClick={() => action.mutate({ kind: "cancel" })} disabled={busy}>
                  <XCircle /> Cancel class
                </Button>
                <Button variant="accent" onClick={() => action.mutate({ kind: "start" })} disabled={!info.can_start || busy} title={info.start_blocked_reason ?? undefined}>
                  {busy ? <Loader2 className="animate-spin" /> : <Play />} Start Class
                </Button>
              </>
            )}
            {info.status === "active" && (
              <Button variant="primary" onClick={() => action.mutate({ kind: "close" })} disabled={busy || tally.unmarked > 0} title={tally.unmarked ? "Mark every student before closing" : undefined}>
                {busy ? <Loader2 className="animate-spin" /> : <Square />} Close Class
              </Button>
            )}
          </div>
        </div>
        <dl className="mt-5 grid grid-cols-2 gap-4 border-t border-border pt-4 sm:grid-cols-5">
          <Fact label="Date" value={formatDate(info.scheduled_start)} />
          <Fact label="Scheduled" value={`${info.start_local}–${info.end_local}`} />
          <Fact label="Room" value={info.room} />
          <Fact label="Students" value={String(tally.roster)} />
          <Fact
            label="Session"
            value={
              info.actual_started_at
                ? `Started ${formatTime(info.actual_started_at)}${info.actual_closed_at ? ` · Closed ${formatTime(info.actual_closed_at)}` : ""}`
                : status.label
            }
          />
        </dl>
        {info.status === "scheduled" && !info.can_start && info.start_blocked_reason && <p className="mt-3 text-xs text-muted">{info.start_blocked_reason}</p>}
      </Card>

      {error && <ErrorState message={error} />}
      {info.status === "closed" && <Notice tone="success">This class is closed. Its attendance has been added to every student's course attendance.</Notice>}

      <Card>
        <CardHeader
          title="Attendance"
          description={
            editable
              ? "Mark all present, then change individual students. Close the class once everyone is marked."
              : info.status === "scheduled"
                ? "Start the class to open attendance."
                : "Recorded attendance for this class."
          }
          action={
            editable && (
              <Button variant="outline" size="sm" onClick={() => action.mutate({ kind: "markAll", status: "present" })} disabled={busy}>
                <CheckCheck /> Mark all present
              </Button>
            )
          }
        />
        <CardBody className="space-y-4">
          <div className="flex flex-wrap gap-2 text-xs">
            <Badge tone="success">{tally.present} present</Badge>
            <Badge tone="warning">{tally.late} late</Badge>
            <Badge tone="danger">{tally.absent} absent</Badge>
            <Badge tone="info">{tally.excused} excused</Badge>
            <Badge tone="neutral">{tally.unmarked} not marked</Badge>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-muted">
                  <th className="py-2 pr-3 font-medium">Student</th>
                  <th className="py-2 pr-3 font-medium">Course attendance</th>
                  <th className="py-2 font-medium">Today</th>
                </tr>
              </thead>
              <tbody>
                {detail.roster.map((entry) => (
                  <tr key={entry.student_id} className="border-b border-border last:border-0">
                    <td className="py-2.5 pr-3">
                      <div className="font-medium">{entry.full_name}</div>
                      <div className="text-xs text-muted">{entry.student_id}</div>
                    </td>
                    <td className="py-2.5 pr-3 tabular-nums">
                      {entry.current_percentage != null ? `${entry.current_percentage.toFixed(1)}%` : "—"}
                      {entry.classes_conducted != null && <span className="ml-1 text-xs text-muted">({entry.classes_attended}/{entry.classes_conducted})</span>}
                      {entry.standing !== "unknown" && entry.standing !== "good" && (
                        <Badge tone={STANDING[entry.standing].tone} className="ml-2">
                          {STANDING[entry.standing].label}
                        </Badge>
                      )}
                    </td>
                    <td className="py-2.5">
                      <MarkControl entry={entry} editable={editable} disabled={busy} onMark={(s) => action.mutate({ kind: "mark", marks: [{ student_id: entry.student_id, status: s }] })} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {detail.required_percentage != null && <p className="text-xs text-muted">Course attendance is compared against the {detail.required_percentage}% attendance policy requirement.</p>}
        </CardBody>
      </Card>
    </div>
  );
}

export function FacultyClassPage() {
  const { sessionId } = useParams();
  const id = Number(sessionId);
  const { data, isLoading, isError, error, refetch } = useFacultyClass(id);
  if (!Number.isFinite(id)) return <Navigate to="/faculty/classes" replace />;
  if (isLoading) return <SkeletonRows rows={6} />;
  if (isError) return <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />;
  return data ? <Workspace detail={data} sessionId={id} /> : null;
}
