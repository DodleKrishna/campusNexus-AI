import { ArrowLeft } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Skeleton } from "@/components/ui/skeleton";

/** Every page's header: optional back link, title, one line of context, actions. */
export function PageTitle({
  title,
  description,
  action,
  back,
}: {
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  back?: { to: string; label: string };
}) {
  return (
    <header className="space-y-3">
      {back && (
        <Link to={back.to} className="inline-flex items-center gap-1.5 text-sm text-muted transition-colors hover:text-ink">
          <ArrowLeft className="size-4" /> {back.label}
        </Link>
      )}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold tracking-tight text-balance text-ink">{title}</h1>
          {description && <p className="mt-1 max-w-3xl text-sm text-muted">{description}</p>}
        </div>
        {action && <div className="flex shrink-0 flex-wrap items-center gap-2">{action}</div>}
      </div>
    </header>
  );
}

/** Dashboard greeting: "Good morning, Aditi", a context line and a small date; skeleton while loading. */
export function DashboardHeader({ title, context, date, aside }: { title?: ReactNode; context?: ReactNode; date?: ReactNode; aside?: ReactNode }) {
  return (
    <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      {title ? (
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold tracking-tight text-ink">{title}</h1>
          {context && <p className="mt-1 text-sm text-muted">{context}</p>}
          {date && <p className="mt-0.5 text-xs text-subtle">{date}</p>}
        </div>
      ) : (
        <div className="space-y-2">
          <Skeleton className="h-7 w-64 max-w-full" />
          <Skeleton className="h-4 w-80 max-w-full" />
        </div>
      )}
      {aside && <div className="flex shrink-0 flex-wrap items-center gap-2 text-sm text-muted">{aside}</div>}
    </header>
  );
}
