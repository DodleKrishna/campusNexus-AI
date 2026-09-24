import { ChevronDown } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/utils/cn";

export function FactCard({ title, children, aside, className }: { title: ReactNode; children?: ReactNode; aside?: ReactNode; className?: string }) {
  return (
    <div className={cn("rounded-lg border border-border bg-surface px-4 py-3", className)}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 text-sm font-semibold">{title}</div>
        {aside}
      </div>
      {children && <div className="mt-1.5 space-y-1 text-sm text-muted">{children}</div>}
    </div>
  );
}

export function Metric({ label, value, tone }: { label: string; value: ReactNode; tone?: "danger" | "success" }) {
  return (
    <div className="rounded-lg bg-surface-muted px-3 py-2">
      <div className="text-[11px] font-medium uppercase tracking-wide text-subtle">{label}</div>
      <div className={cn("mt-0.5 text-lg font-semibold tabular-nums", tone === "danger" && "text-danger-strong", tone === "success" && "text-success")}>{value}</div>
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
  const body = <p className="whitespace-pre-line text-sm leading-relaxed text-ink">{text}</p>;
  if (!collapsed && text.length <= collapseOver) return body;
  return (
    <details className="group rounded-lg border border-border px-3 py-2">
      <summary className="flex cursor-pointer list-none items-center justify-between text-xs font-medium text-muted">
        Agent's full written answer
        <ChevronDown className="size-4 transition-transform group-open:rotate-180" />
      </summary>
      <div className="mt-2">{body}</div>
    </details>
  );
}
