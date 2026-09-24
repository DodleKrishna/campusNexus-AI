import { agentByKey } from "@/features/agents/catalog";
import { SpecialistFacts } from "@/features/SpecialistFacts";
import type { AgentQueryResponse } from "@/types/api";

/**
 * The Enquiry Agent's synthesized lines (built server-side only from verified
 * specialist facts), with each consulted specialist's structured details below.
 */
export function EnquiryAnswer({ response }: { response: AgentQueryResponse }) {
  const verified = response.consulted.filter((c) => c.verification_status === "verified");
  const sources = [...new Set(verified.map((c) => agentByKey(c.agent_key)?.name ?? c.agent_key))];
  return (
    <div className="space-y-3">
      <ul className="space-y-1.5">
        {response.answer
          .split("\n")
          .filter(Boolean)
          .map((line) => (
            <li key={line} className="flex gap-2 text-sm leading-relaxed">
              <span className="mt-2 size-1.5 shrink-0 rounded-full bg-accent" />
              <span>{line}</span>
            </li>
          ))}
      </ul>
      {verified.length > 0 && (
        <details className="group rounded-lg border border-border px-3 py-2">
          <summary className="cursor-pointer list-none text-xs font-medium text-muted">Consulted: {sources.join(", ")} (show details)</summary>
          <div className="mt-3 space-y-4">
            {verified.map((c, i) => (
              <section key={`${c.agent_key}-${i}`} className="space-y-2">
                <h4 className="text-xs font-semibold uppercase tracking-wide text-subtle">
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
