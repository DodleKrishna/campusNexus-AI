import { AlertTriangle, ChevronDown } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ActivityTrail, TrailPending } from "@/nexus/components/ActivityTrail";
import { CampusMark } from "@/nexus/components/brand";
import { Pill } from "@/nexus/components/primitives";
import { MISSION_STATUS_LABELS, missionPhase } from "@/nexus/lib/activity";
import type { ChatTurn } from "@/nexus/state/AssistantContext";
import { cn } from "@/utils/cn";

function VoiceChip({ seconds }: { seconds?: number }) {
  return (
    <span className="inline-flex items-center gap-2 text-[13px] text-mist">
      <span className="flex h-4 items-end gap-[2px]" aria-hidden>
        {[5, 9, 13, 8, 11, 6, 10].map((h, i) => (
          <span key={i} className="w-[2px] rounded-full bg-cyan/80" style={{ height: h }} />
        ))}
      </span>
      Voice message{seconds ? ` · ${seconds.toFixed(1)} s` : ""}
    </span>
  );
}

function NexusTurn({ turn, size }: { turn: ChatTurn; size: "lg" | "sm" }) {
  const [open, setOpen] = useState(true);
  const phase = turn.status ? missionPhase(turn.status) : null;
  return (
    <div className="flex gap-3 animate-rise-sm">
      <CampusMark className={cn("mt-0.5", size === "lg" ? "size-8" : "size-7")} />
      <div className="min-w-0 flex-1">
        {turn.pending && <TrailPending />}
        {turn.error && (
          <div className="flex items-start gap-2 rounded-xl bg-rose/10 px-3.5 py-2.5 text-[13px] text-rose ring-1 ring-rose/25 ring-inset" role="alert">
            <AlertTriangle className="mt-0.5 size-4 shrink-0" />
            <span>{turn.error}</span>
          </div>
        )}
        {!turn.pending && !turn.error && (
          <>
            <p className={cn("whitespace-pre-wrap text-frost", size === "lg" ? "text-[15px] leading-relaxed" : "text-sm leading-relaxed")}>{turn.text}</p>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              {phase === "waiting" && turn.status && <Pill tone="amber">{MISSION_STATUS_LABELS[turn.status]}</Pill>}
              {phase === "failed" && turn.status && <Pill tone="rose">{MISSION_STATUS_LABELS[turn.status]}</Pill>}
              {turn.note && <span className="text-xs text-haze">{turn.note}</span>}
            </div>
            {turn.trail && turn.trail.length > 1 && (
              <div className="mt-3 rounded-xl border border-line bg-white/[0.015] px-3 py-2.5">
                <button
                  type="button"
                  onClick={() => setOpen((v) => !v)}
                  aria-expanded={open}
                  className="flex w-full cursor-pointer items-center justify-between gap-2 text-[11px] font-semibold tracking-[0.12em] text-haze uppercase hover:text-mist"
                >
                  Agent activity · {turn.trail.length - 1} step{turn.trail.length === 2 ? "" : "s"}
                  <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} />
                </button>
                {open && <ActivityTrail items={turn.trail} className="mt-2.5" compact={size === "sm"} />}
                {turn.missionId && open && (
                  <Link to={`/nexus/missions?focus=${turn.missionId}`} className="mt-2 inline-block text-xs text-cyan/90 hover:text-cyan">
                    Mission #{turn.missionId} →
                  </Link>
                )}
              </div>
            )}
            {turn.trailState === "loading" && <div className="mt-3"><TrailPending label="Loading agent activity" /></div>}
          </>
        )}
      </div>
    </div>
  );
}

function UserTurn({ turn, size }: { turn: ChatTurn; size: "lg" | "sm" }) {
  return (
    <div className="flex justify-end animate-rise-sm">
      <div
        className={cn(
          "max-w-[85%] rounded-2xl rounded-br-md bg-gradient-to-br from-electric/25 to-violet/20 px-4 py-2.5 text-frost ring-1 ring-electric/25 ring-inset",
          size === "lg" ? "text-[15px]" : "text-sm",
        )}
      >
        {turn.via === "voice" ? <VoiceChip seconds={turn.durationSec} /> : <span className="whitespace-pre-wrap">{turn.text}</span>}
      </div>
    </div>
  );
}

/** The conversation. Scrolls itself to the newest turn. */
export function Transcript({ turns, size = "lg", className }: { turns: ChatTurn[]; size?: "lg" | "sm"; className?: string }) {
  const end = useRef<HTMLDivElement>(null);
  const last = turns[turns.length - 1];
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns.length, last?.pending, last?.trailState]);

  return (
    <div className={cn("space-y-6", className)} aria-live="polite" aria-label="Conversation with Nexus">
      {turns.map((turn) => (turn.role === "user" ? <UserTurn key={turn.id} turn={turn} size={size} /> : <NexusTurn key={turn.id} turn={turn} size={size} />))}
      <div ref={end} />
    </div>
  );
}
