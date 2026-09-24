import { Badge } from "@/components/ui/badge";
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
    <FactCard
      title={course ? `${course.course_title} (${code})` : code || "Attendance"}
      aside={<Badge tone={standing.tone}>{standing.label}</Badge>}
    >
      <div className="mt-2 grid grid-cols-3 gap-2">
        <Metric label="Current" value={current !== null ? `${current}%` : "—"} tone={eligible ? "success" : "danger"} />
        <Metric label="Required" value={required !== null ? `${required}%` : "—"} />
        <Metric label="Attended" value={`${attendance.classes_attended} / ${attendance.classes_conducted}`} />
      </div>
      {!eligible && needed !== null && (
        <p className="pt-1 text-ink">Attend {needed} consecutive classes to reach {required}%.</p>
      )}
      {eligible && spare !== null && <p className="pt-1 text-ink">You can miss up to {spare} more class{spare === 1 ? "" : "es"} and stay eligible.</p>}
      {exam && (
        <p className="flex items-center gap-2 pt-1">
          Exam eligibility (attendance-based): <Badge tone={exam.tone}>{exam.label}</Badge>
        </p>
      )}
      {policy && (
        <p className="pt-1 text-xs">
          Source: {policy.title}
          {policy.policy_version ? ` ${policy.policy_version}` : ""}
          {policy.section ? ` · ${policy.section}` : ""}
        </p>
      )}
    </FactCard>
  );
}

function TimetableFacts({ facts }: { facts: Facts }) {
  const slots = asArray<Slot>(facts.timetable);
  if (!("timetable" in facts)) return null;
  const today = todayIndex();
  const byDay = WEEKDAYS.map((day, index) => ({ day, index, slots: slots.filter((s) => s.weekday === index) })).filter((d) => d.slots.length);
  if (!byDay.length) return <FactCard title="Timetable">No classes on your timetable.</FactCard>;
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {byDay.map((d) => (
        <FactCard key={d.day} title={d.day} aside={d.index === today ? <Badge tone="accent">Today</Badge> : undefined}>
          {d.slots.map((s) => (
            <div key={`${s.course_code}-${s.start_time}`} className="flex justify-between gap-2">
              <span className="text-ink">
                {s.start_time}–{s.end_time} · {s.course_title}
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
    <div className="grid gap-2 sm:grid-cols-2">
      {exams.map((e) => (
        <FactCard key={`${e.course_code}-${e.scheduled_start}`} title={e.course_title} aside={<Badge tone="info">{titleCase(e.exam_type)}</Badge>}>
          <div className="text-ink">
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
