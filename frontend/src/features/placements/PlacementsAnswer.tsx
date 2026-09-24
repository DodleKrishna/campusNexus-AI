import { Badge, type BadgeTone } from "@/components/ui/badge";
import { asArray } from "@/features/facts";
import { AnswerText, FactCard } from "@/features/shared";
import type { Facts } from "@/types/api";
import { formatDate, titleCase } from "@/utils/format";

interface Skill { name: string }
interface Eligibility {
  status: string;
  opportunity: { opportunity_id: number; title: string; company: string; opportunity_type: string; deadline: string | null; minimum_cgpa: number };
  blocking_reasons: string[];
  missing_mandatory_skills: (Skill | string)[];
  missing_recommended_skills: (Skill | string)[];
  already_applied: boolean;
  application_status: string | null;
}

const TONES: Record<string, BadgeTone> = { eligible: "success", not_eligible: "danger", deadline_passed: "neutral" };
const skillName = (s: Skill | string) => (typeof s === "string" ? s : s.name);

export function PlacementsAnswer({ facts, answer }: { facts: Facts; answer: string }) {
  const eligibilities = asArray<Eligibility>(facts.eligibilities);
  const gaps = asArray<string>(facts.skill_gaps);
  const eligibleFirst = [...eligibilities].sort((a, b) => Number(b.status === "eligible") - Number(a.status === "eligible"));
  return (
    <div className="space-y-3">
      {gaps.length > 0 && (
        <FactCard title="Skills to develop">
          <div className="flex flex-wrap gap-1.5 pt-1">
            {gaps.map((gap) => (
              <Badge key={gap} tone="warning">{gap}</Badge>
            ))}
          </div>
        </FactCard>
      )}
      {eligibleFirst.length > 0 && (
        <div className="grid gap-2 sm:grid-cols-2">
          {eligibleFirst.map((e) => {
            const missing = [...e.missing_mandatory_skills, ...e.missing_recommended_skills].map(skillName);
            return (
              <FactCard
                key={e.opportunity.opportunity_id}
                title={
                  <span>
                    {e.opportunity.title}
                    <span className="block text-xs font-normal text-muted">{e.opportunity.company}</span>
                  </span>
                }
                aside={<Badge tone={TONES[e.status] ?? "neutral"}>{titleCase(e.status)}</Badge>}
              >
                <div>
                  {titleCase(e.opportunity.opportunity_type)} · min CGPA {e.opportunity.minimum_cgpa}
                  {e.opportunity.deadline ? ` · apply by ${formatDate(e.opportunity.deadline)}` : ""}
                </div>
                {e.already_applied && <div className="text-ink">Application: {titleCase(e.application_status ?? "submitted")}</div>}
                {e.blocking_reasons.length > 0 && <div className="text-danger-strong">{e.blocking_reasons[0]}</div>}
                {missing.length > 0 && <div className="text-xs">Missing skills: {missing.join(", ")}</div>}
              </FactCard>
            );
          })}
        </div>
      )}
      <AnswerText text={answer} collapsed={eligibilities.length > 0 || gaps.length > 0} />
    </div>
  );
}
