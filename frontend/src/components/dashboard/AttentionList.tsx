import { ChevronRight } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { StatusDot, type BadgeTone } from "@/components/ui/badge";
import { Card, CardHeader } from "@/components/ui/card";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/ui/states";

export interface AttentionItem {
  id: string;
  tone: BadgeTone;
  /** Short category shown before the title, e.g. "Attendance". */
  kind: string;
  title: string;
  detail?: ReactNode;
  to?: string;
}

/**
 * "Needs your attention": a short, scannable list of items whose status the backend
 * already decided (below requirement, pending, delayed, past SLA...). Nothing is scored here.
 */
export function AttentionList({
  items,
  isLoading,
  title = "Needs your attention",
  emptyTitle = "You're all caught up.",
  emptyDescription = "Nothing needs your attention right now.",
  action,
  limit = 6,
}: {
  items: AttentionItem[];
  isLoading?: boolean;
  title?: string;
  emptyTitle?: string;
  emptyDescription?: string;
  action?: ReactNode;
  limit?: number;
}) {
  const shown = items.slice(0, limit);
  return (
    <Card>
      <CardHeader
        title={title}
        description={items.length ? `${items.length} item${items.length === 1 ? "" : "s"}` : undefined}
        action={action}
      />
      {isLoading && !items.length ? (
        <SkeletonRows rows={3} className="px-5 pb-5" />
      ) : shown.length === 0 ? (
        <EmptyState compact title={emptyTitle} description={emptyDescription} />
      ) : (
        <ul className="divide-y divide-border border-t border-border">
          {shown.map((item) => {
            const body = (
              <div className="flex items-start gap-3 px-5 py-3">
                <StatusDot tone={item.tone} className="mt-1.5" />
                <div className="min-w-0 flex-1">
                  <p className="text-xs text-muted">{item.kind}</p>
                  <p className="text-sm font-medium text-ink">{item.title}</p>
                  {item.detail && <p className="mt-0.5 text-sm text-muted">{item.detail}</p>}
                </div>
                {item.to && <ChevronRight aria-hidden className="mt-3 size-4 shrink-0 text-subtle" />}
              </div>
            );
            return (
              <li key={item.id}>
                {item.to ? (
                  <Link to={item.to} className="block transition-colors hover:bg-surface-muted/60">
                    {body}
                  </Link>
                ) : (
                  body
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}
