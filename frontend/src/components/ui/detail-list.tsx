import type { ReactNode } from "react";
import { cn } from "@/utils/cn";

/** Clean labelled rows: label on the left, value on the right (stacks on narrow screens). */
export function DetailList({ children, className }: { children: ReactNode; className?: string }) {
  return <dl className={cn("divide-y divide-border", className)}>{children}</dl>;
}

export function DetailRow({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <div className={cn("flex flex-col gap-0.5 py-2.5 sm:flex-row sm:items-baseline sm:justify-between sm:gap-6", className)}>
      <dt className="shrink-0 text-sm text-muted">{label}</dt>
      <dd className="min-w-0 text-sm font-medium text-ink sm:text-right">{children}</dd>
    </div>
  );
}

/** A compact label-over-value field for summary grids. */
export function Field({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <div className={cn("min-w-0", className)}>
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 text-sm font-medium text-ink">{children}</dd>
    </div>
  );
}
