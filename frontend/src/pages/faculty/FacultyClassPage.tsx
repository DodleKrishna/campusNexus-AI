import { CheckCheck, Loader2, Square, Users, XCircle, Play } from "lucide-react";
import { Navigate, useParams } from "react-router-dom";
import { ApiError } from "@/api/client";
import { Avatar } from "@/components/ui/avatar";
import { StatusBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTable } from "@/components/ui/data-table";
import { ProgressBar } from "@/components/ui/progress";
import { Skeleton, SkeletonTable } from "@/components/ui/skeleton";
import { EmptyState, ErrorState, Notice } from "@/components/ui/states";
import { MARK, SESSION, STANDING, statusOf } from "@/components/dashboard/status";
import { useClassAction, useFacultyClass } from "@/hooks/useFacultyData";
import { PageTitle } from "@/pages/PageTitle";
import type { FacultyClassDetail, MarkStatus, RosterEntry } from "@/types/api";
import { cn } from "@/utils/cn";
import { formatDate, formatTime } from "@/utils/format";

const MARK_OPTIONS: { status: MarkStatus; short: string }[] = [
  { status: "present", short: "P" },
  { status: "absent", short: "A" },
  { status: "late", short: "L" },
  { status: "excused", short: "E" },
];
const MARK_SELECTED: Record<MarkStatus, string> = {
  present: "bg-success text-white",
  absent: "bg-danger text-white",
  late: "bg-warning text-white",
  excused: "bg-muted text-white",
};

function MarkControl({ entry, editable, disabled, onMark }: { entry: RosterEntry; editable: boolean; disabled: boolean; onMark: (status: MarkStatus) => void }) {
  if (!editable) {
    return entry.mark ? <StatusBadge {...MARK[entry.mark]} /> : <span className="text-sm text-muted">Not marked</span>;
  }
  return (
    <div role="radiogroup" aria-label={`Attendance for ${entry.full_name}`} className="inline-flex rounded-md border border-border bg-surface p-0.5">
      {MARK_OPTIONS.map(({ status, short }) => {
        const selected = entry.mark === status;
        return (
          <button
            key={status}
            type="button"
            role="radio"
            aria-checked={selected}
            aria-label={MARK[status].label}
            title={MARK[status].label}
            disabled={disabled}
            onClick={() => !selected && onMark(status)}
            className={cn(
              "h-8 w-9 rounded text-[13px] font-semibold transition-colors duration-150 disabled:opacity-60",
              selected ? MARK_SELECTED[status] : "text-muted hover:bg-surface-muted hover:text-ink",
            )}
          >
            {short}
          </button>
        );
      })}
    </div>
  );
}

function Workspace({ detail, sessionId }: { detail: FacultyClassDetail; sessionId: number }) {
  const action = useClassAction(sessionId);
  const info = detail.class_info;
  const { tally } = info;
  const status = statusOf(SESSION, info.status);
  const editable = info.status === "active";
  const busy = action.isPending;
  const marked = tally.roster - tally.unmarked;
  const error = action.error instanceof ApiError ? action.error.message : action.error ? "That didn't work. Please try again." : null;

  return (
    <div className="space-y-6">
      <PageTitle
        back={{ to: "/faculty/classes", label: "My classes" }}
        title={info.course_title}
        description={
          <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
            <StatusBadge {...status} label={info.status === "active" ? "LIVE" : status.label} />
            <span className="font-medium text-ink">
              {info.department_code} {info.year}-{info.section}
            </span>
            <span aria-hidden>·</span>
            <span className="tabular-nums">
              {info.start_local}–{info.end_local}
            </span>
            <span aria-hidden>·</span>
            <span>{info.room}</span>
            <span aria-hidden>·</span>
            <span>{formatDate(info.scheduled_start)}</span>
            {info.is_extra_class && <span>· Extra class</span>}
          </span>
        }
        action={
          <>
            {info.status === "scheduled" && (
              <>
                <Button variant="ghost" onClick={() => action.mutate({ kind: "cancel" })} disabled={busy}>
                  <XCircle /> Cancel session
                </Button>
                <Button onClick={() => action.mutate({ kind: "start" })} disabled={!info.can_start || busy} title={info.start_blocked_reason ?? undefined}>
                  {busy ? <Loader2 className="animate-spin" /> : <Play />} Start Class
                </Button>
              </>
            )}
            {info.status === "active" && (
              <Button onClick={() => action.mutate({ kind: "close" })} disabled={busy || tally.unmarked > 0} title={tally.unmarked ? "Mark every student before closing" : undefined}>
                {busy ? <Loader2 className="animate-spin" /> : <Square />} Close Class
              </Button>
            )}
          </>
        }
      />

      {info.status === "scheduled" && !info.can_start && info.start_blocked_reason && <Notice>{info.start_blocked_reason}</Notice>}
      {error && <ErrorState message={error} />}
      {info.status === "closed" && (
        <Notice tone="success">
          Class closed{info.actual_closed_at ? ` at ${formatTime(info.actual_closed_at)}` : ""}. Its attendance has been added to every student's course attendance.
        </Notice>
      )}

      <section className="rounded-card border border-border bg-surface">
        {info.status !== "scheduled" && info.status !== "cancelled" && (
          <div className="flex flex-col gap-3 border-b border-border px-5 py-4 sm:flex-row sm:items-center sm:gap-6">
            <div className="shrink-0">
              <p className="text-xs text-muted">Attendance progress</p>
              <p className="text-xl font-semibold tabular-nums text-ink">
                {marked} / {tally.roster}
              </p>
            </div>
            <div className="min-w-0 flex-1 space-y-2">
              <ProgressBar value={marked} max={tally.roster || 1} tone="accent" label="Students marked" />
              <p className="flex flex-wrap gap-x-4 text-xs text-muted tabular-nums">
                <span>{tally.present} present</span>
                <span>{tally.absent} absent</span>
                <span>{tally.late} late</span>
                <span>{tally.excused} excused</span>
                {info.actual_started_at && <span>Started {formatTime(info.actual_started_at)}</span>}
              </p>
            </div>
            {editable && detail.roster.length > 0 && (
              <Button variant="outline" size="sm" onClick={() => action.mutate({ kind: "markAll", status: "present" })} disabled={busy}>
                <CheckCheck /> Mark all present
              </Button>
            )}
          </div>
        )}
        {info.status === "scheduled" && <p className="border-b border-border px-5 py-3 text-sm text-muted">Start the class to open attendance.</p>}
        {detail.roster.length === 0 ? (
          <EmptyState compact icon={<Users />} title="No students on this roster" />
        ) : (
          <DataTable sticky columns={["Student", "Roll No.", "Attendance", "Today's status"]} caption={`${info.course_title} roster`}>
            {detail.roster.map((entry) => (
              <tr key={entry.student_id} className={cn(editable && !entry.mark && "bg-warning-soft/30")}>
                <td>
                  <div className="flex items-center gap-3">
                    <Avatar name={entry.full_name} />
                    <span className="font-medium text-ink">{entry.full_name}</span>
                  </div>
                </td>
                <td className="text-muted tabular-nums">{entry.student_id}</td>
                <td>
                  <span className="inline-flex items-center gap-2">
                    <span className="tabular-nums">{entry.current_percentage != null ? `${entry.current_percentage.toFixed(1)}%` : "—"}</span>
                    {(entry.standing === "at_risk" || entry.standing === "below_requirement") && <StatusBadge {...STANDING[entry.standing]} />}
                  </span>
                </td>
                <td>
                  <MarkControl entry={entry} editable={editable} disabled={busy} onMark={(s) => action.mutate({ kind: "mark", marks: [{ student_id: entry.student_id, status: s }] })} />
                </td>
              </tr>
            ))}
          </DataTable>
        )}
        <p className="border-t border-border px-5 py-3 text-xs text-muted">
          P Present · A Absent · L Late · E Excused
          {detail.required_percentage != null && ` · Course attendance is compared against the ${detail.required_percentage}% requirement.`}
        </p>
      </section>
    </div>
  );
}

export function FacultyClassPage() {
  const { sessionId } = useParams();
  const id = Number(sessionId);
  const { data, isLoading, isError, error, refetch } = useFacultyClass(id);
  if (!Number.isFinite(id)) return <Navigate to="/faculty/classes" replace />;
  if (isLoading)
    return (
      <div className="space-y-6" role="status" aria-label="Loading class">
        <Skeleton className="h-8 w-72 max-w-full" />
        <Skeleton className="h-4 w-96 max-w-full" />
        <SkeletonTable rows={8} />
      </div>
    );
  if (isError) return <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />;
  return data ? <Workspace detail={data} sessionId={id} /> : null;
}
