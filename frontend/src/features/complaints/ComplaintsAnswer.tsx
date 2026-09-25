import { StatusBadge } from "@/components/ui/badge";
import { asArray } from "@/features/facts";
import { AnswerText, FactCard } from "@/features/shared";
import type { Facts } from "@/types/api";
import { formatDateTime, titleCase } from "@/utils/format";

interface CaseAssessment {
  case: { case_code: string; category: string; status: string; priority: string; department: string; description: string };
  response_due_at: string;
  resolution_due_at: string;
  response_breached: boolean;
  resolution_breached: boolean;
}

export function ComplaintsAnswer({ facts, answer }: { facts: Facts; answer: string }) {
  const cases = asArray<CaseAssessment>(facts.case_assessments);
  return (
    <div className="space-y-3">
      {cases.length > 0 && (
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {cases.map((c) => {
            const breached = c.response_breached || c.resolution_breached;
            return (
              <FactCard
                key={c.case.case_code}
                title={
                  <span>
                    {c.case.case_code}
                    <span className="block text-xs font-normal text-muted">
                      {titleCase(c.case.category)} · {c.case.department}
                    </span>
                  </span>
                }
                aside={<StatusBadge label={breached ? "SLA breached" : "Within SLA"} tone={breached ? "danger" : "success"} />}
              >
                <div className="text-ink">
                  {titleCase(c.case.status)} · {titleCase(c.case.priority)} priority
                </div>
                <div className={c.response_breached ? "text-danger-strong" : undefined}>Response due {formatDateTime(c.response_due_at)}</div>
                <div className={c.resolution_breached ? "text-danger-strong" : undefined}>Resolution due {formatDateTime(c.resolution_due_at)}</div>
              </FactCard>
            );
          })}
        </div>
      )}
      <AnswerText text={answer} collapsed={cases.length > 0} />
    </div>
  );
}
