import { useId } from "react";
import { cn } from "@/utils/cn";

export const PRODUCT_NAME = "CAMPUS AI";
export const PRODUCT_FULL_NAME = "Collaborative Autonomous Multi-Agent Platform for University Services";
export const PRODUCT_TAGLINE = "One Campus. Many Agents. One Intelligence.";

/**
 * The CAMPUS AI mark: one intelligence (the core) joined to many agents (the
 * three orbiting nodes). ``animated`` slowly turns the agent triangle.
 */
export function CampusMark({ className, animated = false }: { className?: string; animated?: boolean }) {
  const id = useId().replace(/:/g, "");
  return (
    <svg viewBox="0 0 32 32" aria-hidden className={cn("size-8 shrink-0", className)}>
      <defs>
        <linearGradient id={`${id}g`} x1="4" y1="4" x2="28" y2="28" gradientUnits="userSpaceOnUse">
          <stop stopColor="#22D3EE" />
          <stop offset=".55" stopColor="#3B82F6" />
          <stop offset="1" stopColor="#8B5CF6" />
        </linearGradient>
        <radialGradient id={`${id}c`} cx="0.5" cy="0.5" r="0.5">
          <stop stopColor="#E0F7FF" />
          <stop offset=".5" stopColor="#22D3EE" />
          <stop offset="1" stopColor="#3B82F6" />
        </radialGradient>
      </defs>
      <rect width="32" height="32" rx="9" fill="#0B1226" />
      <rect x=".5" y=".5" width="31" height="31" rx="8.5" fill="none" stroke="rgb(148 163 255 / 0.18)" />
      <circle cx="16" cy="16" r="9.5" fill="none" stroke={`url(#${id}g)`} strokeWidth="1.2" opacity=".45" />
      <g className={animated ? "origin-center animate-spin-slow [transform-box:fill-box]" : undefined} style={{ transformOrigin: "16px 16px" }}>
        <path d="M16 6.5 24.2 20.75H7.8Z" fill="none" stroke={`url(#${id}g)`} strokeWidth="1.5" strokeLinejoin="round" opacity=".9" />
        <circle cx="16" cy="6.5" r="1.9" fill="#22D3EE" />
        <circle cx="24.2" cy="20.75" r="1.9" fill="#8B5CF6" />
        <circle cx="7.8" cy="20.75" r="1.9" fill="#3B82F6" />
      </g>
      <circle cx="16" cy="16" r="3.3" fill={`url(#${id}c)`} />
    </svg>
  );
}

/** Mark + wordmark for the dark surfaces. */
export function CampusLogo({ className, compact = false }: { className?: string; compact?: boolean }) {
  return (
    <div className={cn("flex min-w-0 items-center gap-2.5", className)}>
      <CampusMark className="size-9" />
      {!compact && (
        <div className="min-w-0 leading-none">
          <div className="font-display text-[15px] font-bold tracking-[0.08em] text-frost">
            CAMPUS <span className="nx-text-gradient">AI</span>
          </div>
          <div className="mt-1 truncate text-[10.5px] font-medium tracking-[0.12em] text-haze uppercase">Multi-agent campus OS</div>
        </div>
      )}
    </div>
  );
}
