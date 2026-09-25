import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/utils/cn";

type Tone = "default" | "danger" | "warning" | "success";

const ICON_TONE: Record<Tone, string> = {
  default: "bg-surface-muted text-muted",
  danger: "bg-danger-soft text-danger-strong",
  warning: "bg-warning-soft text-warning",
  success: "bg-success-soft text-success",
};
const VALUE_TONE: Record<Tone, string> = {
  default: "text-ink",
  danger: "text-danger-strong",
  warning: "text-warning",
  success: "text-ink",
};

/**
 * The one metric card: icon, small label, large value, optional one-line context.
 * ``compact`` is the dense strip variant. Label and value always share a parent.
 */
export function MetricCard({
  label,
  value,
  context,
  icon,
  tone = "default",
  to,
  compact = false,
}: {
  label: string;
  value: ReactNode;
  context?: ReactNode;
  icon?: ReactNode;
  tone?: Tone;
  to?: string;
  compact?: boolean;
}) {
  const body = compact ? (
    <div className={cn("flex h-full items-center gap-3 rounded-card border border-border bg-surface px-4 py-3", to && "transition-colors hover:border-border-strong")}>
      {icon && <span className={cn("flex size-8 shrink-0 items-center justify-center rounded-lg [&_svg]:size-4", ICON_TONE[tone])}>{icon}</span>}
      <div className="flex min-w-0 flex-1 items-baseline justify-between gap-3">
        <p className="truncate text-sm text-muted">{label}</p>
        <p className={cn("text-lg font-semibold tabular-nums", VALUE_TONE[tone])}>{value}</p>
      </div>
    </div>
  ) : (
    <div
      className={cn(
        "flex h-full items-start gap-3 rounded-card border border-border bg-surface p-4 shadow-[var(--shadow-card)] sm:p-5",
        to && "transition-colors hover:border-border-strong",
      )}
    >
      <div className="min-w-0 flex-1">
        <p className="truncate text-xs font-medium text-muted">{label}</p>
        <p className={cn("mt-1.5 truncate text-2xl font-semibold tracking-tight tabular-nums", VALUE_TONE[tone])}>{value}</p>
        {context && <p className="mt-1 truncate text-xs text-muted">{context}</p>}
      </div>
      {icon && <span className={cn("hidden size-9 shrink-0 items-center justify-center rounded-lg xl:flex [&_svg]:size-[18px]", ICON_TONE[tone])}>{icon}</span>}
    </div>
  );
  return to ? (
    <Link to={to} className="block min-w-0 rounded-card">
      {body}
    </Link>
  ) : (
    <div className="min-w-0">{body}</div>
  );
}

export function MetricGrid({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4", className)}>{children}</div>;
}

export function MetricSkeletons({ count = 4, className }: { count?: number; className?: string }) {
  return (
    <MetricGrid className={className}>
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="space-y-3 rounded-card border border-border bg-surface p-5" aria-hidden>
          <Skeleton className="h-3 w-20" />
          <Skeleton className="h-7 w-16" />
          <Skeleton className="h-3 w-28" />
        </div>
      ))}
    </MetricGrid>
  );
}
