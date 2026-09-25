import { CalendarClock, ShieldCheck } from "lucide-react";
import { Badge, StatusBadge } from "@/components/ui/badge";
import { ProgressBar } from "@/components/ui/progress";
import { ELIGIBILITY, STANDING } from "@/components/dashboard/status";
import { asArray, asNumber, asRecord } from "@/features/facts";
import { AnswerText, FactCard, Metric } from "@/features/shared";
import { useAttendance } from "@/hooks/useStudentData";
import type { Evidence, Facts } from "@/types/api";
import { formatDate, formatTime, titleCase } from "@/utils/format";

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

interface Slot { course_code: string; course_title: string; weekday: number; start_time: string; end_time: string; location: string }
interface Exam { course_code: string; course_title: string; exam_type: string; scheduled_start: string; scheduled_end: string; location: string }

function todayIndex(): number {
  const name = new Intl.DateTimeFormat("en-US", { timeZone: "Asia/Kolkata", weekday: "long" }).format(new Date());
  return WEEKDAYS.indexOf(name);
}

/** Attendance result: the number first, then requirement, status, recovery and the policy it was checked against. */
function AttendanceFacts({ facts, evidence }: { facts: Facts; evidence: Evidence[] }) {
  const { data: courses } = useAttendance();
  const attendance = asRecord(facts.attendance);
  const threshold = asRecord(facts.threshold);
  const eligibility = asRecord(facts.eligibility);
  const code = String(asRecord(facts.course_resolution)?.course_code ?? "");
  if (!attendance || attendance.status !== "ok") return null;

  const course = courses?.find((c) => c.course_code === code);
  const current = asNumber(attendance.current_percentage);
  const required = asNumber(attendance.required_percentage);
  const eligible = attendance.eligible_now === true;
  const needed = asNumber(attendance.classes_needed_to_reach_threshold);
  const spare = asNumber(attendance.maximum_additional_absences_allowed);
  const policy = evidence.find((e) => e.document_id === threshold?.document_id) ?? evidence[0];
  const standing = course ? STANDING[course.standing] : eligible ? STANDING.good : STANDING.below_requirement;
  const exam = eligibility ? ELIGIBILITY[String(eligibility.status)] : null;

  return (
    <div className="min-w-0 overflow-hidden rounded-card border border-border bg-surface">
      <div className="flex items-start justify-between gap-3 px-4 pt-4">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-ink">{course?.course_title ?? (code || "Attendance")}</p>
          {course && <p className="text-xs text-muted">{code}</p>}
        </div>
        <StatusBadge {...standing} />
      </div>
      <div className="px-4 pt-3 pb-4">
        <div className="flex items-baseline gap-2">
          <span className={eligible ? "text-3xl font-semibold tracking-tight tabular-nums text-ink" : "text-3xl font-semibold tracking-tight tabular-nums text-danger-strong"}>
            {current !== null ? `${current}%` : "—"}
          </span>
          <span className="text-sm text-muted">current attendance</span>
        </div>
        {current !== null && <ProgressBar className="mt-3" value={current} marker={required} tone={eligible ? "success" : "danger"} label="Current attendance against the requirement" />}
      </div>
      <div className="grid grid-cols-2 divide-x divide-border border-t border-border sm:grid-cols-3">
        <Metric label="Classes attended" value={`${attendance.classes_attended} / ${attendance.classes_conducted}`} />
        <Metric label="Required" value={required !== null ? `${required}%` : "—"} />
        {exam && (
          <div className="col-span-2 border-t border-border px-4 py-3 sm:col-span-1 sm:border-t-0">
            <StatusBadge {...exam} />
            <div className="mt-1 text-xs text-muted">Exam eligibility</div>
          </div>
        )}
      </div>
      {(needed !== null && !eligible) || (eligible && spare !== null) ? (
        <div className="border-t border-border bg-surface-muted/60 px-4 py-3">
          <p className="text-xs text-muted">{eligible ? "Margin" : "Recovery"}</p>
          {!eligible && needed !== null && <p className="text-sm font-medium text-ink">Attend {needed} consecutive classes to reach {required}%.</p>}
          {eligible && spare !== null && <p className="text-sm font-medium text-ink">You can miss up to {spare} more class{spare === 1 ? "" : "es"} and stay eligible.</p>}
        </div>
      ) : null}
      {policy && (
        <p className="flex items-center gap-1.5 border-t border-border px-4 py-2.5 text-xs text-muted">
          <ShieldCheck aria-hidden className="size-3.5 shrink-0 text-success" />
          <span>
            Source: {policy.title}
            {policy.policy_version ? ` ${policy.policy_version}` : ""}
            {policy.section ? ` · ${policy.section}` : ""}
          </span>
        </p>
      )}
    </div>
  );
}

function TimetableFacts({ facts }: { facts: Facts }) {
  const slots = asArray<Slot>(facts.timetable);
  if (!("timetable" in facts)) return null;
  const today = todayIndex();
  const byDay = WEEKDAYS.map((day, index) => ({ day, index, slots: slots.filter((s) => s.weekday === index) })).filter((d) => d.slots.length);
  if (!byDay.length) return <FactCard title="Timetable">No classes on your timetable.</FactCard>;
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
      {byDay.map((d) => (
        <FactCard key={d.day} title={d.day} aside={d.index === today ? <Badge tone="accent">Today</Badge> : undefined}>
          {d.slots.map((s) => (
            <div key={`${s.course_code}-${s.start_time}`} className="flex justify-between gap-3">
              <span className="min-w-0 text-ink">
                <span className="tabular-nums">
                  {s.start_time}–{s.end_time}
                </span>{" "}
                · {s.course_title}
              </span>
              <span className="shrink-0 text-xs">{s.location}</span>
            </div>
          ))}
        </FactCard>
      ))}
    </div>
  );
}

function ExamFacts({ facts }: { facts: Facts }) {
  if (!("exams" in facts)) return null;
  const exams = asArray<Exam>(facts.exams);
  if (!exams.length) return <FactCard title="Exams">No exams scheduled.</FactCard>;
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
      {exams.map((e) => (
        <FactCard key={`${e.course_code}-${e.scheduled_start}`} title={e.course_title} aside={<Badge tone="neutral">{titleCase(e.exam_type)}</Badge>}>
          <div className="flex items-center gap-1.5 text-ink">
            <CalendarClock aria-hidden className="size-3.5 text-subtle" />
            {formatDate(e.scheduled_start)} · {formatTime(e.scheduled_start)}–{formatTime(e.scheduled_end)} IST
          </div>
          <div>{e.location}</div>
        </FactCard>
      ))}
    </div>
  );
}

export function AcademicAnswer({ facts, evidence, answer }: { facts: Facts; evidence: Evidence[]; answer: string }) {
  const hasCards = asRecord(facts.attendance)?.status === "ok" || "timetable" in facts || "exams" in facts;
  return (
    <div className="space-y-3">
      <AttendanceFacts facts={facts} evidence={evidence} />
      <TimetableFacts facts={facts} />
      <ExamFacts facts={facts} />
      <AnswerText text={answer} collapsed={hasCards} />
    </div>
  );
}
