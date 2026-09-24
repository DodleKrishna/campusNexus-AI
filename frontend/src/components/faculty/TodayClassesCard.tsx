import { CalendarClock, ClipboardCheck, Eye, Loader2, Play, Square } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { buttonVariants } from "@/components/ui/button-variants";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { SESSION } from "@/components/dashboard/status";
import { classPath } from "@/components/faculty/paths";
import { useClassAction } from "@/hooks/useFacultyData";
import type { FacultyClass } from "@/types/api";
import { cn } from "@/utils/cn";
import { formatTime } from "@/utils/format";

function ClassActions({ item }: { item: FacultyClass }) {
  const navigate = useNavigate();
  const action = useClassAction(item.session_id);
  const busy = action.isPending;
  const error = action.error instanceof ApiError ? action.error.message : action.error ? "That didn't work. Please try again." : null;

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex flex-wrap justify-end gap-2">
        {item.status === "scheduled" && (
          <Button
            variant="accent"
            size="sm"
            disabled={!item.can_start || busy}
            title={item.start_blocked_reason ?? undefined}
            onClick={() => action.mutate({ kind: "start" }, { onSuccess: () => navigate(classPath(item)) })}
          >
            {busy ? <Loader2 className="animate-spin" /> : <Play />} Start Class
          </Button>
        )}
        {item.status === "active" && (
          <>
            <Link to={classPath(item)} className={buttonVariants({ variant: "accent", size: "sm" })}>
              <ClipboardCheck /> Open Attendance
            </Link>
            <Link to={classPath(item)} className={buttonVariants({ variant: "outline", size: "sm" })}>
              <Square /> Close Class
            </Link>
          </>
        )}
        {item.status === "closed" && (
          <Link to={classPath(item)} className={buttonVariants({ variant: "outline", size: "sm" })}>
            <Eye /> View Attendance
          </Link>
        )}
      </div>
      {item.status === "scheduled" && !item.can_start && item.start_blocked_reason && <p className="max-w-xs text-right text-[11px] text-muted">{item.start_blocked_reason}</p>}
      {error && <p className="max-w-xs text-right text-xs text-danger-strong">{error}</p>}
    </div>
  );
}

export function ClassRow({ item }: { item: FacultyClass }) {
  const status = SESSION[item.status];
  const { tally } = item;
  return (
    <li className={cn("flex flex-wrap items-center gap-4 rounded-lg border px-4 py-3", item.status === "active" ? "border-accent/40 bg-accent-soft" : "border-border")}>
      <div className="w-28 shrink-0 text-sm font-semibold tabular-nums">
        {item.start_local}–{item.end_local}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <Link to={classPath(item)} className="truncate text-sm font-medium hover:underline">
            {item.course_title}
          </Link>
          <Badge tone={status.tone}>{status.label}</Badge>
          {item.is_extra_class && <Badge tone="neutral">Extra class</Badge>}
        </div>
        <div className="truncate text-xs text-muted">
          {item.course_code} · {item.department_code} Year {item.year}, Section {item.section} · {item.room} · {tally.roster} students
        </div>
        {(item.status === "active" || item.status === "closed") && (
          <div className="mt-0.5 text-xs text-muted">
            {item.actual_started_at && <>Started {formatTime(item.actual_started_at)} · </>}
            {tally.present + tally.late} present, {tally.absent} absent{tally.unmarked ? `, ${tally.unmarked} not marked` : ""}
          </div>
        )}
      </div>
      <ClassActions item={item} />
    </li>
  );
}

export function TodayClassesCard({ classes, isLoading, error, onRetry }: { classes?: FacultyClass[]; isLoading?: boolean; error?: Error | null; onRetry?: () => void }) {
  return (
    <Card>
      <CardHeader icon={<CalendarClock />} title="Today's classes" description="Times in IST · a class can be started up to 15 minutes early" />
      <CardBody>
        {isLoading && <SkeletonRows rows={2} />}
        {error && <ErrorState message={`CampusNexus couldn't load your classes. ${error.message}`} onRetry={onRetry} />}
        {classes && classes.length === 0 && <EmptyState title="No classes today" description="None of your teaching assignments meets today." />}
        {classes && classes.length > 0 && (
          <ol className="space-y-2">
            {classes.map((item) => (
              <ClassRow key={item.session_id} item={item} />
            ))}
          </ol>
        )}
      </CardBody>
    </Card>
  );
}
