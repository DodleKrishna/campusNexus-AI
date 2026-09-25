import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { ModeBadge } from "@/components/layout/ModeBadge";

/** Compact assistant-panel header: icon, name, one line of scope, AI mode and capability. */
export function AgentHeader({
  icon: Icon,
  title,
  tagline,
  capability,
  action,
}: {
  icon: LucideIcon;
  title: string;
  tagline: string;
  capability?: string;
  action?: ReactNode;
}) {
  return (
    <header className="flex items-center gap-3 border-b border-border pb-4">
      <span className="flex size-9 shrink-0 items-center justify-center rounded-md bg-primary-soft text-primary">
        <Icon className="size-[18px]" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <h1 className="text-lg font-semibold tracking-tight text-ink">{title}</h1>
          <ModeBadge />
          {capability && <span className="text-xs text-muted">{capability}</span>}
        </div>
        <p className="truncate text-[13px] text-muted">{tagline}</p>
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </header>
  );
}

/** A restrained "working" indicator for agent replies: three dots and a short status line. */
export function ThinkingIndicator({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2.5 text-sm text-muted" role="status">
      <span className="flex gap-1" aria-hidden>
        {[0, 1, 2].map((i) => (
          <span key={i} className="size-1.5 rounded-full bg-primary animate-thinking" style={{ animationDelay: `${i * 160}ms` }} />
        ))}
      </span>
      {label}
    </div>
  );
}
