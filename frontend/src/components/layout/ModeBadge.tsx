import { Cpu, Radio } from "lucide-react";
import { useHealth } from "@/hooks/useStudentData";
import { cn } from "@/utils/cn";

const PROVIDERS: Record<string, string> = { groq: "Groq", anthropic: "Anthropic" };

/** The backend's *actual* LLM mode, from /health -- deterministic output is never shown as live AI. */
export function ModeBadge() {
  const { data } = useHealth();
  if (!data) return null;
  const live = data.llm.live;
  const title = live
    ? `Live AI: ${PROVIDERS[data.llm.provider] ?? data.llm.provider}${data.llm.model ? ` · ${data.llm.model}` : ""}`
    : "Local deterministic mode: no external AI is called";
  return (
    <span
      title={title}
      className={cn(
        "hidden items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wide md:inline-flex",
        live ? "bg-accent-soft text-accent-hover" : "bg-surface-muted text-muted",
      )}
    >
      {live ? <Radio className="size-3.5" /> : <Cpu className="size-3.5" />}
      {live ? "Live AI" : "Local / Deterministic"}
    </span>
  );
}
