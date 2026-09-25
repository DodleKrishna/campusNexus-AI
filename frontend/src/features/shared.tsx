import { ChevronDown } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/utils/cn";

/** One structured result card in an agent reply. */
export function FactCard({ title, children, aside, className }: { title: ReactNode; children?: ReactNode; aside?: ReactNode; className?: string }) {
  return (
    <div className={cn("min-w-0 rounded-lg border border-border bg-surface px-4 py-3", className)}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 text-sm font-semibold text-ink">{title}</div>
        {aside && <div className="shrink-0">{aside}</div>}
      </div>
      {children && <div className="mt-1.5 space-y-1 text-sm text-muted">{children}</div>}
    </div>
  );
}

/** A labelled number inside a result card. */
export function Metric({ label, value, tone }: { label: string; value: ReactNode; tone?: "danger" | "success" }) {
  return (
    <div className="min-w-0 px-4 py-3">
      <div className={cn("text-lg font-semibold tabular-nums text-ink", tone === "danger" && "text-danger-strong", tone === "success" && "text-success")}>{value}</div>
      <div className="mt-0.5 text-xs text-muted">{label}</div>
    </div>
  );
}

/**
 * The agent's own written answer. Shown inline when it is the main content;
 * collapsed when structured cards above already present the same facts
 * (``collapsed``) or when it is long.
 */
export function AnswerText({ text, collapsed = false, collapseOver = 420 }: { text: string; collapsed?: boolean; collapseOver?: number }) {
  if (!text.trim()) return null;
  const body = <p className="text-sm leading-relaxed whitespace-pre-line text-ink">{text}</p>;
  if (!collapsed && text.length <= collapseOver) return body;
  return (
    <details className="group">
      <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 text-sm font-medium text-muted transition-colors hover:text-ink">
        Read the written explanation
        <ChevronDown aria-hidden className="size-4 transition-transform duration-150 group-open:rotate-180" />
      </summary>
      <div className="mt-2 rounded-lg border border-border bg-surface px-4 py-3">{body}</div>
    </details>
  );
}
