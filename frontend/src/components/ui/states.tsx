import { AlertTriangle, Inbox, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/utils/cn";

export function EmptyState({ title, description, icon, className }: { title: string; description?: string; icon?: ReactNode; className?: string }) {
  return (
    <div className={cn("flex flex-col items-center justify-center rounded-lg border border-dashed border-border px-4 py-8 text-center", className)}>
      <div className="mb-2 text-subtle [&_svg]:size-5">{icon ?? <Inbox />}</div>
      <p className="text-sm font-medium text-ink">{title}</p>
      {description && <p className="mt-1 max-w-sm text-xs text-muted">{description}</p>}
    </div>
  );
}

export function ErrorState({ message, onRetry, className }: { message: string; onRetry?: () => void; className?: string }) {
  return (
    <div role="alert" className={cn("flex items-start gap-3 rounded-lg border border-danger/20 bg-danger-soft px-4 py-3", className)}>
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-danger-strong" />
      <div className="flex-1 text-sm text-ink">{message}</div>
      {onRetry && (
        <Button variant="ghost" size="sm" onClick={onRetry}>
          <RefreshCw /> Retry
        </Button>
      )}
    </div>
  );
}

export function Notice({ tone = "info", children, className }: { tone?: "info" | "warning" | "success"; children: ReactNode; className?: string }) {
  const tones = {
    info: "border-info/15 bg-info-soft text-ink",
    warning: "border-warning/20 bg-warning-soft text-ink",
    success: "border-success/20 bg-success-soft text-ink",
  };
  return <div className={cn("rounded-lg border px-4 py-3 text-sm", tones[tone], className)}>{children}</div>;
}
