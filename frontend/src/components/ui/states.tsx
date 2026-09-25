import { AlertTriangle, CheckCircle2, Info, RefreshCw, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/utils/cn";

/** A designed empty state: a soft icon, a short title, one line of context, optional action. */
export function EmptyState({
  title,
  description,
  icon,
  action,
  className,
  compact = false,
}: {
  title: string;
  description?: string;
  icon?: ReactNode;
  action?: ReactNode;
  className?: string;
  compact?: boolean;
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center text-center", compact ? "px-4 py-6" : "px-6 py-10", className)}>
      <div className="mb-3 flex size-10 items-center justify-center rounded-full bg-surface-muted text-subtle [&_svg]:size-5">{icon ?? <CheckCircle2 />}</div>
      <p className="text-sm font-medium text-ink">{title}</p>
      {description && <p className="mt-1 max-w-sm text-sm text-muted">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ErrorState({ message, onRetry, className }: { message: string; onRetry?: () => void; className?: string }) {
  return (
    <div role="alert" className={cn("flex items-start gap-3 rounded-lg border border-danger/20 bg-danger-soft px-4 py-3", className)}>
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-danger-strong" />
      <div className="min-w-0 flex-1 text-sm text-ink">{message}</div>
      {onRetry && (
        <Button variant="ghost" size="sm" onClick={onRetry} className="-my-1">
          <RefreshCw /> Retry
        </Button>
      )}
    </div>
  );
}

const NOTICE = {
  info: { box: "border-info/15 bg-info-soft", icon: <Info className="text-info" /> },
  warning: { box: "border-warning/20 bg-warning-soft", icon: <TriangleAlert className="text-warning" /> },
  success: { box: "border-success/20 bg-success-soft", icon: <CheckCircle2 className="text-success" /> },
};

export function Notice({ tone = "info", children, className, icon = true }: { tone?: "info" | "warning" | "success"; children: ReactNode; className?: string; icon?: boolean }) {
  return (
    <div className={cn("flex items-start gap-2.5 rounded-lg border px-4 py-3 text-sm text-ink", NOTICE[tone].box, className)}>
      {icon && <span className="mt-0.5 shrink-0 [&_svg]:size-4">{NOTICE[tone].icon}</span>}
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}
