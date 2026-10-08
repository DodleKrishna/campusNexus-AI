/**
 * Small building blocks for the CAMPUS AI dark surfaces (shadcn/ui-style: a
 * thin, typed wrapper with variants and a ``className`` escape hatch).
 */
import type { VariantProps } from "class-variance-authority";
import { forwardRef, type ButtonHTMLAttributes, type HTMLAttributes, type ReactNode } from "react";
import { nxButton } from "@/nexus/components/variants";
import { cn } from "@/utils/cn";

export type NxButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & VariantProps<typeof nxButton>;

export const NxButton = forwardRef<HTMLButtonElement, NxButtonProps>(function NxButton({ className, variant, size, type = "button", ...props }, ref) {
  return <button ref={ref} type={type} className={cn(nxButton({ variant, size }), className)} {...props} />;
});

/** A frosted panel. ``ring`` adds the cyan→violet gradient edge. */
export function GlassPanel({ className, ring = false, ...props }: HTMLAttributes<HTMLDivElement> & { ring?: boolean }) {
  return <div className={cn("nx-glass rounded-2xl", ring && "nx-ring", className)} {...props} />;
}

export function SectionLabel({ children, action, className }: { children: ReactNode; action?: ReactNode; className?: string }) {
  return (
    <div className={cn("flex items-center justify-between gap-3", className)}>
      <h3 className="text-[11px] font-semibold tracking-[0.14em] text-haze uppercase">{children}</h3>
      {action}
    </div>
  );
}

export type Tone = "cyan" | "violet" | "signal" | "amber" | "rose" | "dim";

const TONE_DOT: Record<Tone, string> = {
  cyan: "bg-cyan",
  violet: "bg-violet",
  signal: "bg-signal",
  amber: "bg-amber",
  rose: "bg-rose",
  dim: "bg-dim",
};

const TONE_CHIP: Record<Tone, string> = {
  cyan: "bg-cyan/10 text-cyan ring-cyan/25",
  violet: "bg-violet/12 text-[#c4b5fd] ring-violet/30",
  signal: "bg-signal/10 text-signal ring-signal/25",
  amber: "bg-amber/10 text-amber ring-amber/25",
  rose: "bg-rose/10 text-rose ring-rose/25",
  dim: "bg-glass-strong text-haze ring-line",
};

/** A status dot; ``live`` adds a soft radar ping. */
export function StatusDot({ tone = "cyan", live = false, className }: { tone?: Tone; live?: boolean; className?: string }) {
  return (
    <span className={cn("relative inline-flex size-2 shrink-0", className)} aria-hidden>
      {live && <span className={cn("absolute inset-0 rounded-full animate-ping-soft", TONE_DOT[tone])} />}
      <span className={cn("relative inline-flex size-2 rounded-full", TONE_DOT[tone])} />
    </span>
  );
}

export function Pill({ tone = "dim", children, className }: { tone?: Tone; children: ReactNode; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset", TONE_CHIP[tone], className)}>
      {children}
    </span>
  );
}

/** A single shimmering placeholder line for loading rails. */
export function GlowSkeleton({ className }: { className?: string }) {
  return <div className={cn("h-3 rounded-full bg-[linear-gradient(90deg,rgb(255_255_255/0.04),rgb(255_255_255/0.09),rgb(255_255_255/0.04))] bg-[length:200%_100%] animate-shimmer", className)} />;
}

/** The quiet empty state used across the rails and lists. */
export function QuietEmpty({ icon, title, hint, className }: { icon?: ReactNode; title: string; hint?: string; className?: string }) {
  return (
    <div className={cn("flex items-start gap-3 rounded-xl border border-dashed border-line px-3.5 py-3", className)}>
      {icon && <span className="mt-0.5 text-dim [&_svg]:size-4">{icon}</span>}
      <div className="min-w-0">
        <p className="text-[13px] text-mist">{title}</p>
        {hint && <p className="mt-0.5 text-xs text-haze">{hint}</p>}
      </div>
    </div>
  );
}
