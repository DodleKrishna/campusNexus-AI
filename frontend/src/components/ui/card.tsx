import type { HTMLAttributes, ReactNode } from "react";
import { cn } from "@/utils/cn";

/** The single surface style: white, a hairline border, 12px radius. Never nest cards. */
export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("min-w-0 rounded-card border border-border bg-surface shadow-[var(--shadow-card)]", className)} {...props} />;
}

export function CardHeader({
  title,
  description,
  icon,
  action,
  className,
  as: Heading = "h2",
}: {
  title: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  action?: ReactNode;
  className?: string;
  as?: "h2" | "h3";
}) {
  return (
    <div className={cn("flex items-start justify-between gap-3 px-5 pt-4 pb-3", className)}>
      <div className="flex min-w-0 items-start gap-2.5">
        {icon && <div className="mt-0.5 text-subtle [&_svg]:size-4">{icon}</div>}
        <div className="min-w-0">
          <Heading className="text-sm font-semibold text-ink">{title}</Heading>
          {description && <p className="mt-0.5 text-xs text-muted">{description}</p>}
        </div>
      </div>
      {action && <div className="flex shrink-0 items-center gap-2">{action}</div>}
    </div>
  );
}

export function CardBody({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("px-5 pb-5 first:pt-5", className)} {...props} />;
}

/** A section heading that sits directly on the page background (no card). */
export function SectionHeading({ title, description, action, className }: { title: ReactNode; description?: ReactNode; action?: ReactNode; className?: string }) {
  return (
    <div className={cn("flex flex-wrap items-end justify-between gap-2", className)}>
      <div className="min-w-0">
        <h2 className="text-base font-semibold text-ink">{title}</h2>
        {description && <p className="mt-0.5 text-sm text-muted">{description}</p>}
      </div>
      {action}
    </div>
  );
}
