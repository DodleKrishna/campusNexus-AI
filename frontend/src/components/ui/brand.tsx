import { cn } from "@/utils/cn";

/** The CampusNexus mark: a blue tile with a connected apex (matches the favicon). */
export function BrandMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" aria-hidden className={cn("size-8 shrink-0", className)}>
      <rect width="32" height="32" rx="8" fill="var(--color-primary)" />
      <path d="M9 20.5 16 10l7 10.5" fill="none" stroke="var(--color-surface)" strokeWidth="2.8" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="16" cy="22.2" r="2.1" fill="var(--color-surface)" />
    </svg>
  );
}

/** Mark + wordmark. ``tone="light"`` is for the dark navigation. */
export function Logo({ tone = "dark", caption, className, markClassName }: { tone?: "dark" | "light"; caption?: string; className?: string; markClassName?: string }) {
  return (
    <div className={cn("flex min-w-0 items-center gap-2.5", className)}>
      <BrandMark className={markClassName} />
      <div className="min-w-0 leading-tight">
        <div className={cn("truncate text-[15px] font-semibold tracking-tight", tone === "light" ? "text-white" : "text-ink")}>
          CampusNexus <span className={tone === "light" ? "text-accent-bright" : "text-primary"}>AI</span>
        </div>
        {caption && <div className={cn("truncate text-xs", tone === "light" ? "text-nav-text" : "text-muted")}>{caption}</div>}
      </div>
    </div>
  );
}
