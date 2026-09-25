import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";
import { cn } from "@/utils/cn";

const badgeVariants = cva("inline-flex max-w-full items-center gap-1.5 whitespace-nowrap rounded-md px-1.5 py-0.5 text-xs font-medium leading-4", {
  variants: {
    tone: {
      neutral: "bg-surface-muted text-muted",
      primary: "bg-primary-soft text-primary-hover",
      accent: "bg-accent-soft text-accent-hover",
      live: "bg-primary-soft text-primary-hover",
      success: "bg-success-soft text-success-strong",
      warning: "bg-warning-soft text-warning-strong",
      caution: "bg-caution-soft text-caution-strong",
      danger: "bg-danger-soft text-danger-strong",
      info: "bg-info-soft text-primary-hover",
    },
  },
  defaultVariants: { tone: "neutral" },
});

export type BadgeTone = NonNullable<VariantProps<typeof badgeVariants>["tone"]>;

export function Badge({ className, tone, ...props }: HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants>) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}

const DOT: Record<BadgeTone, string> = {
  neutral: "bg-subtle",
  primary: "bg-primary",
  accent: "bg-accent",
  live: "bg-primary",
  success: "bg-success",
  warning: "bg-warning",
  caution: "bg-caution",
  danger: "bg-danger",
  info: "bg-info",
};

/**
 * The one status badge (small only): a coloured dot plus a text label, so a status is
 * never conveyed by colour alone. ``pulse`` marks something happening right now.
 */
export function StatusBadge({
  label,
  tone = "neutral",
  pulse,
  className,
  title,
}: {
  label: string;
  tone?: BadgeTone;
  pulse?: boolean;
  className?: string;
  title?: string;
}) {
  return (
    <Badge tone={tone} className={className} title={title}>
      <span aria-hidden className={cn("size-1.5 shrink-0 rounded-full", DOT[tone], pulse && "animate-pulse")} />
      {label}
    </Badge>
  );
}

/** A bare status dot for dense lists; always pair it with visible text. */
export function StatusDot({ tone = "neutral", pulse, className }: { tone?: BadgeTone; pulse?: boolean; className?: string }) {
  return <span aria-hidden className={cn("inline-block size-2 shrink-0 rounded-full", DOT[tone], pulse && "animate-pulse", className)} />;
}
