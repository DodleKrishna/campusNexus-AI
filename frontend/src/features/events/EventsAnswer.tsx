import { Badge, type BadgeTone } from "@/components/ui/badge";
import { asArray } from "@/features/facts";
import { AnswerText, FactCard } from "@/features/shared";
import type { Facts } from "@/types/api";
import { formatDate, formatTime } from "@/utils/format";

interface Conflict { course_code: string; exam_type?: string }
interface Assessment {
  event: { event_id: number; title: string; location: string; start_at: string; end_at: string; category: string };
  availability: string;
  already_registered: boolean;
  conflict_check_performed: boolean;
  timetable_conflicts: Conflict[];
  exam_conflicts: Conflict[];
  matched_terms: string[];
  matched_skill_gaps: string[];
}

function status(a: Assessment): { label: string; tone: BadgeTone; detail?: string } {
  if (a.already_registered) return { label: "Registered", tone: "success" };
  const clashes = [...a.timetable_conflicts.map((c) => `${c.course_code} class`), ...a.exam_conflicts.map((c) => `${c.course_code} ${c.exam_type ?? ""} exam`.replace("  ", " "))];
  if (clashes.length) return { label: "Clash", tone: "danger", detail: `Clashes with ${clashes.join(", ")}` };
  if (a.availability === "full") return { label: "Full", tone: "danger" };
  if (a.availability === "registration_closed") return { label: "Closed", tone: "neutral" };
  if (a.availability === "not_open") return { label: "Not open yet", tone: "neutral" };
  if (!a.conflict_check_performed) return { label: "Not checked", tone: "warning", detail: "Schedule conflicts were not checked" };
  return { label: "No clash", tone: "success", detail: "No clash with your classes or exams · registration open" };
}

export function EventsAnswer({ facts, answer }: { facts: Facts; answer: string }) {
  const assessments = asArray<Assessment>(facts.assessments);
  return (
    <div className="space-y-3">
      {assessments.length > 0 && (
        <div className="grid gap-2 sm:grid-cols-2">
          {assessments.map((a) => {
            const s = status(a);
            const matched = a.matched_skill_gaps.length ? a.matched_skill_gaps : a.matched_terms;
            return (
              <FactCard key={a.event.event_id} title={a.event.title} aside={<Badge tone={s.tone}>{s.label}</Badge>}>
                <div className="text-ink">
                  {formatDate(a.event.start_at)} · {formatTime(a.event.start_at)}–{formatTime(a.event.end_at)} IST
                </div>
                <div>{a.event.location}</div>
                {s.detail && <div className={s.tone === "danger" ? "text-danger-strong" : undefined}>{s.detail}</div>}
                {matched.length > 0 && <div className="text-xs">Matches: {matched.join(", ")}</div>}
              </FactCard>
            );
          })}
        </div>
      )}
      <p className="text-xs text-muted">Recommendations only. Registering goes through the selection and approval workflow, never directly from chat.</p>
      <AnswerText text={answer} collapsed={assessments.length > 0} />
    </div>
  );
}
