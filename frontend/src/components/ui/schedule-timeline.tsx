import type { ReactNode } from "react";
import { StatusBadge } from "@/components/ui/badge";
import type { StatusInfo } from "@/components/dashboard/status";
import { cn } from "@/utils/cn";

export interface TimelineItem {
  key: string;
  start: string;
  end: string;
  title: ReactNode;
  subtitle?: ReactNode;
  status: StatusInfo;
  /** Emphasise the item happening now. */
  current?: boolean;
  muted?: boolean;
  action?: ReactNode;
}

const RAIL: Record<string, string> = {
  live: "bg-primary ring-primary/20",
  success: "bg-success ring-success/15",
  caution: "bg-caution ring-caution/15",
  danger: "bg-danger ring-danger/15",
  warning: "bg-warning ring-warning/15",
};

/** A clean vertical schedule: time, a rail with a status node, the class, its status and an optional action. */
export function ScheduleTimeline({ items }: { items: TimelineItem[] }) {
  return (
    <ol className="relative">
      {items.map((item, index) => (
        <li key={item.key} className={cn("relative flex gap-3 px-5 py-3 sm:gap-4", item.current && "bg-primary-soft/60")}>
          <div className="w-12 shrink-0 pt-0.5 text-right tabular-nums sm:w-14">
            <p className={cn("text-sm font-semibold", item.muted ? "text-muted" : "text-ink")}>{item.start}</p>
            <p className="text-xs text-subtle">{item.end}</p>
          </div>
          <div aria-hidden className="relative flex w-3 shrink-0 justify-center">
            <span className={cn("absolute w-px bg-border", index === 0 ? "top-3" : "-top-3", index === items.length - 1 ? "h-3" : "bottom-[-0.75rem]")} />
            <span className={cn("relative mt-1.5 size-2.5 rounded-full ring-4", RAIL[item.status.tone] ?? "bg-border-strong ring-surface-muted", item.status.pulse && "animate-pulse")} />
          </div>
          <div className="flex min-w-0 flex-1 flex-col gap-2 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
            <div className="min-w-0">
              <div className={cn("truncate text-sm font-medium", item.muted ? "text-muted" : "text-ink")}>{item.title}</div>
              {item.subtitle && <div className="truncate text-[13px] text-muted">{item.subtitle}</div>}
            </div>
            <div className="flex shrink-0 flex-wrap items-center gap-2">
              <StatusBadge {...item.status} />
              {item.action}
            </div>
          </div>
        </li>
      ))}
    </ol>
  );
}
