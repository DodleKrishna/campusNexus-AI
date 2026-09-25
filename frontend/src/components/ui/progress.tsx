import { cn } from "@/utils/cn";

const TONE = { accent: "bg-accent", success: "bg-success", warning: "bg-warning", danger: "bg-danger" };

/** A thin progress bar with an optional marker (e.g. the required percentage). */
export function ProgressBar({
  value,
  max = 100,
  tone = "accent",
  marker,
  label,
  className,
}: {
  value: number;
  max?: number;
  tone?: keyof typeof TONE;
  marker?: number | null;
  label: string;
  className?: string;
}) {
  const pct = max > 0 ? Math.min(100, Math.max(0, (value / max) * 100)) : 0;
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={max}
      aria-valuenow={value}
      className={cn("relative h-1.5 w-full rounded-full bg-surface-muted", className)}
    >
      <div className={cn("h-full rounded-full transition-[width] duration-200", TONE[tone])} style={{ width: `${pct}%` }} />
      {marker != null && <div aria-hidden className="absolute -top-1 h-3.5 w-0.5 rounded-full bg-ink/40" style={{ left: `${Math.min(100, (marker / max) * 100)}%` }} />}
    </div>
  );
}
