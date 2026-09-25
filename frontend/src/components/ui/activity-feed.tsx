import { Bell, BookOpen, CalendarDays, ClipboardCheck, Cpu, FileCheck2, MessageSquareWarning, Sparkles, type LucideIcon } from "lucide-react";
import { relativeTime } from "@/utils/format";
import { cn } from "@/utils/cn";

export type ActivityCategory = "Academic" | "Attendance" | "Request" | "Complaint" | "AI" | "System" | "Event" | "Update";

export interface ActivityItem {
  id: string;
  category: ActivityCategory;
  title: string;
  context?: string;
  /** ISO timestamp from the record the item came from. */
  time: string;
}

const ICONS: Record<ActivityCategory, LucideIcon> = {
  Academic: BookOpen,
  Attendance: ClipboardCheck,
  Request: FileCheck2,
  Complaint: MessageSquareWarning,
  AI: Sparkles,
  System: Cpu,
  Event: CalendarDays,
  Update: Bell,
};

/**
 * A compact vertical feed of real operational signals (audit events, notifications).
 * Callers pass records from the API; nothing here is generated.
 */
export function ActivityFeed({ items, className }: { items: ActivityItem[]; className?: string }) {
  return (
    <ol className={cn("divide-y divide-border", className)}>
      {items.map((item) => {
        const Icon = ICONS[item.category];
        return (
          <li key={item.id} className="flex gap-3 px-5 py-3">
            <span aria-hidden className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-md bg-surface-muted text-muted">
              <Icon className="size-3.5" />
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline justify-between gap-3">
                <p className="text-xs font-medium text-muted">{item.category}</p>
                <time dateTime={item.time} className="shrink-0 text-xs text-subtle">
                  {relativeTime(item.time)}
                </time>
              </div>
              <p className="truncate text-sm font-medium text-ink">{item.title}</p>
              {item.context && <p className="line-clamp-1 text-[13px] text-muted">{item.context}</p>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
