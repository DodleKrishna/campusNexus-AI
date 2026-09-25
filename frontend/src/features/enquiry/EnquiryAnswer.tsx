import { ChevronDown } from "lucide-react";
import { agentByKey } from "@/features/agents/catalog";
import { SpecialistFacts } from "@/features/SpecialistFacts";
import type { AgentQueryResponse } from "@/types/api";

const SHORT: Record<string, string> = { academic: "Academic", events: "Events", placements: "Placement", complaints: "Complaints" };

/** "Classes today (Friday): none on your timetable." -> ["Classes today (Friday)", "none on your timetable."] */
function splitLine(line: string): [string | null, string] {
  const match = /^([^:]{2,40}):\s+(.+)$/.exec(line);
  return match ? [match[1], match[2].replace(/^\w/, (c) => c.toUpperCase())] : [null, line];
}

/**
 * The Enquiry Agent's synthesized lines (built server-side only from verified
 * specialist facts), laid out as "today at a glance", with the services that were
 * checked underneath. Each service's details are one click away; no internal trace.
 */
export function EnquiryAnswer({ response }: { response: AgentQueryResponse }) {
  const verified = response.consulted.filter((c) => c.verification_status === "verified");
  const services = [...new Set(response.consulted.map((c) => c.agent_key))];
  const lines = response.answer.split("\n").filter(Boolean);
  const glance = services.length > 1;
  return (
    <div className="space-y-3">
      <div className="overflow-hidden rounded-card border border-border bg-surface">
        {glance && <p className="border-b border-border px-4 py-2.5 text-sm font-semibold text-ink">Today at a glance</p>}
        <dl className="divide-y divide-border">
          {lines.map((line) => {
            const [label, value] = splitLine(line);
            return label ? (
              <div key={line} className="grid grid-cols-1 gap-0.5 px-4 py-2.5 sm:grid-cols-[10rem_minmax(0,1fr)] sm:gap-4">
                <dt className="text-[13px] font-medium text-muted">{label}</dt>
                <dd className="text-sm text-ink">{value}</dd>
              </div>
            ) : (
              <div key={line} className="px-4 py-2.5">
                <dd className="text-sm leading-relaxed text-ink">{line}</dd>
              </div>
            );
          })}
        </dl>
      </div>
      {services.length > 0 && (
        <p className="text-xs text-muted">
          Checked: <span className="font-medium text-ink">{services.map((key) => SHORT[key] ?? agentByKey(key)?.name ?? key).join(" · ")}</span>
        </p>
      )}
      {verified.length > 0 && (
        <details className="group">
          <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 text-[13px] font-medium text-muted transition-colors hover:text-ink">
            Details from each service
            <ChevronDown aria-hidden className="size-4 transition-transform duration-150 group-open:rotate-180" />
          </summary>
          <div className="mt-3 space-y-5">
            {verified.map((c, i) => (
              <section key={`${c.agent_key}-${i}`} className="space-y-2">
                <h4 className="text-xs font-medium text-muted">
                  {agentByKey(c.agent_key)?.name} · {c.objective}
                </h4>
                <SpecialistFacts answer={c} />
              </section>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}
