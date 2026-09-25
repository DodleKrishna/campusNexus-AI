import { Check, X } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/utils/cn";

export type StepState = "done" | "current" | "failed" | "upcoming";

export interface Step {
  label: string;
  state: StepState;
  detail?: ReactNode;
}

const STATE_TEXT: Record<StepState, string> = { done: "completed", current: "in progress", failed: "stopped", upcoming: "not started" };

function Marker({ state }: { state: StepState }) {
  return (
    <span
      aria-hidden
      className={cn(
        "relative z-10 flex size-6 shrink-0 items-center justify-center rounded-full border text-xs",
        state === "done" && "border-success bg-success text-white",
        state === "current" && "border-primary bg-primary-soft",
        state === "failed" && "border-danger bg-danger text-white",
        state === "upcoming" && "border-border-strong bg-surface",
      )}
    >
      {state === "done" && <Check className="size-3.5" strokeWidth={3} />}
      {state === "failed" && <X className="size-3.5" strokeWidth={3} />}
      {state === "current" && <span className="size-2 rounded-full bg-primary animate-pulse" />}
    </span>
  );
}

/** Workflow progress. Each step states its status in text as well as by its marker. */
export function Stepper({ steps, className, orientation = "vertical" }: { steps: Step[]; className?: string; orientation?: "vertical" | "horizontal" }) {
  if (orientation === "horizontal") {
    return (
      <ol className={cn("flex items-start", className)}>
        {steps.map((step, index) => (
          <li key={`${step.label}-${index}`} className="relative flex min-w-0 flex-1 flex-col items-center text-center">
            {index > 0 && <span aria-hidden className={cn("absolute top-3 right-1/2 h-px w-full", step.state === "upcoming" ? "bg-border" : "bg-success/50")} />}
            <Marker state={step.state} />
            <p className={cn("mt-1.5 px-1 text-xs leading-tight", step.state === "upcoming" ? "text-muted" : "font-medium text-ink")}>
              {step.label}
              <span className="sr-only"> ({STATE_TEXT[step.state]})</span>
            </p>
          </li>
        ))}
      </ol>
    );
  }
  return (
    <ol className={className}>
      {steps.map((step, index) => {
        const last = index === steps.length - 1;
        return (
          <li key={`${step.label}-${index}`} className="relative flex gap-3 pb-4 last:pb-0">
            {!last && <span aria-hidden className={cn("absolute top-6 left-[11px] h-[calc(100%-1.25rem)] w-px", step.state === "done" ? "bg-success/40" : "bg-border")} />}
            <Marker state={step.state} />
            <div className="min-w-0 pt-0.5">
              <p className={cn("text-sm", step.state === "upcoming" ? "text-muted" : "font-medium text-ink")}>
                {step.label}
                <span className="sr-only"> ({STATE_TEXT[step.state]})</span>
              </p>
              {step.detail && <div className="mt-0.5 text-xs text-muted">{step.detail}</div>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
