import { useHealth } from "@/hooks/useStudentData";
import { cn } from "@/utils/cn";

const PROVIDERS: Record<string, string> = { groq: "Groq", anthropic: "Anthropic" };

/** The backend's *actual* LLM mode, from /health -- deterministic output is never shown as live AI. */
export function ModeBadge({ className }: { className?: string }) {
  const { data } = useHealth();
  if (!data) return null;
  const live = data.llm.live;
  const title = live
    ? `Live AI: ${PROVIDERS[data.llm.provider] ?? data.llm.provider}${data.llm.model ? ` · ${data.llm.model}` : ""}`
    : "Deterministic mode: no external AI is called";
  return (
    <span
      title={title}
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-md px-1.5 py-0.5 text-xs font-medium",
        live ? "bg-primary-soft text-primary-hover" : "bg-surface-muted text-muted",
        className,
      )}
    >
      <span aria-hidden className={cn("size-1.5 rounded-full", live ? "animate-pulse bg-primary" : "bg-subtle")} />
      {live ? "Live AI" : "Deterministic mode"}
    </span>
  );
}
