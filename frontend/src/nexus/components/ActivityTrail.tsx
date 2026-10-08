import { Ban, Bot, CheckCircle2, Clock3, Database, Hand, ShieldCheck, Sparkles, type LucideIcon } from "lucide-react";
import type { TrailIcon, TrailItem, TrailTone } from "@/nexus/lib/activity";
import { cn } from "@/utils/cn";

const ICONS: Record<TrailIcon, LucideIcon> = {
  nexus: Sparkles,
  agent: Bot,
  data: Database,
  guardian: ShieldCheck,
  wait: Clock3,
  human: Hand,
  done: CheckCircle2,
  blocked: Ban,
};

const TONES: Record<TrailTone, { icon: string; line: string }> = {
  cyan: { icon: "text-cyan bg-cyan/10 ring-cyan/25", line: "from-cyan/50" },
  violet: { icon: "text-[#c4b5fd] bg-violet/15 ring-violet/30", line: "from-violet/50" },
  signal: { icon: "text-signal bg-signal/10 ring-signal/25", line: "from-signal/50" },
  amber: { icon: "text-amber bg-amber/10 ring-amber/25", line: "from-amber/50" },
  rose: { icon: "text-rose bg-rose/10 ring-rose/25", line: "from-rose/50" },
};

/** The live "Nexus is working" row shown while a request is in flight. */
export function TrailPending({ label = "Nexus analyzing request" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2.5 py-1" role="status" aria-live="polite">
      <span className="relative flex size-6 items-center justify-center rounded-full bg-cyan/10 ring-1 ring-cyan/30 ring-inset">
        <span className="absolute inset-0 rounded-full bg-cyan/20 animate-ping-soft" />
        <Sparkles className="relative size-3.5 text-cyan" />
      </span>
      <span className="nx-shimmer-text text-[13px] font-medium">{label}…</span>
    </div>
  );
}

/**
 * The structured activity behind one reply. Each row is a persisted step
 * (who acted, what kind of action, its status) revealed with a short stagger.
 */
export function ActivityTrail({ items, className, compact = false }: { items: TrailItem[]; className?: string; compact?: boolean }) {
  return (
    <ol className={cn("relative", className)} aria-label="Agent activity">
      {items.map((item, index) => {
        const Icon = ICONS[item.icon];
        const tone = TONES[item.tone];
        const last = index === items.length - 1;
        return (
          <li
            key={item.key}
            className="relative flex gap-2.5 pb-2.5 animate-rise-sm last:pb-0"
            style={{ animationDelay: `${index * 140}ms` }}
          >
            {!last && <span className={cn("absolute top-6 bottom-0 left-[11px] w-px bg-gradient-to-b to-transparent", tone.line)} aria-hidden />}
            <span className={cn("relative z-10 flex size-6 shrink-0 items-center justify-center rounded-full ring-1 ring-inset", tone.icon)}>
              <Icon className="size-3.5" />
            </span>
            <div className={cn("min-w-0 pt-0.5", compact ? "text-xs" : "text-[13px]")}>
              <span className="font-medium text-frost">{item.actor}</span>
              <span className="text-haze"> · {item.label}</span>
              {item.detail && <span className="ml-2 rounded-md bg-glass-strong px-1.5 py-px text-[11px] text-mist ring-1 ring-line ring-inset">{item.detail}</span>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
