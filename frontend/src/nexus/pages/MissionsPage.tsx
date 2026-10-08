import { ChevronDown, Target } from "lucide-react";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ActivityTrail, TrailPending } from "@/nexus/components/ActivityTrail";
import { GlowSkeleton, Pill, QuietEmpty, StatusDot, type Tone } from "@/nexus/components/primitives";
import { useAssistantMissions, useMissionSteps } from "@/nexus/hooks/useNexusData";
import { MISSION_STATUS_LABELS, missionPhase, missionTrail, type MissionPhase } from "@/nexus/lib/activity";
import { PageHeader } from "@/nexus/pages/PageHeader";
import type { AssistantMissionSummary } from "@/types/api";
import { cn } from "@/utils/cn";
import { formatDateTime, relativeTime } from "@/utils/format";

const FILTERS: { key: "all" | MissionPhase; label: string }[] = [
  { key: "all", label: "All" },
  { key: "active", label: "Running" },
  { key: "waiting", label: "Waiting" },
  { key: "done", label: "Completed" },
  { key: "failed", label: "Stopped" },
];

const PHASE_TONE: Record<MissionPhase, Tone> = { active: "cyan", waiting: "amber", done: "signal", failed: "rose" };

function MissionSteps({ missionId }: { missionId: number }) {
  const { data, isLoading, isError } = useMissionSteps(missionId);
  const trail = useMemo(() => (data ? missionTrail(data) : []), [data]);
  if (isLoading) return <TrailPending label="Loading agent activity" />;
  if (isError) return <p className="text-sm text-haze">Agent activity is unavailable for this mission.</p>;
  return <ActivityTrail items={trail} />;
}

function MissionRow({ mission, open, onToggle, index }: { mission: AssistantMissionSummary; open: boolean; onToggle: () => void; index: number }) {
  const phase = missionPhase(mission.status);
  const tone = PHASE_TONE[phase];
  const panelId = `mission-${mission.mission_id}`;
  return (
    <li className="animate-rise-sm" style={{ animationDelay: `${Math.min(index, 10) * 45}ms` }}>
      <div className={cn("nx-glass overflow-hidden rounded-2xl transition-colors", open && "border-line-strong")}>
        <button type="button" onClick={onToggle} aria-expanded={open} aria-controls={panelId} className="flex w-full cursor-pointer items-start gap-4 px-5 py-4 text-left">
          <span className={cn("mt-1 flex size-9 shrink-0 items-center justify-center rounded-xl ring-1 ring-inset", phase === "done" ? "bg-signal/10 ring-signal/25" : phase === "failed" ? "bg-rose/10 ring-rose/25" : phase === "waiting" ? "bg-amber/10 ring-amber/25" : "bg-cyan/10 ring-cyan/25")}>
            <StatusDot tone={tone} live={phase === "active" || phase === "waiting"} />
          </span>
          <div className="min-w-0 flex-1">
            <p className={cn("text-[15px] text-frost", !open && "line-clamp-1")}>{mission.goal}</p>
            <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-haze">
              <span>Mission #{mission.mission_id}</span>
              <span>{mission.step_count} step{mission.step_count === 1 ? "" : "s"}</span>
              <span title={formatDateTime(mission.updated_at)}>Updated {relativeTime(mission.updated_at)}</span>
            </p>
          </div>
          <Pill tone={tone} className="hidden sm:inline-flex">{MISSION_STATUS_LABELS[mission.status]}</Pill>
          <ChevronDown className={cn("mt-2 size-4 shrink-0 text-haze transition-transform", open && "rotate-180")} />
        </button>
        {open && (
          <div id={panelId} className="grid gap-6 border-t border-line px-5 py-5 md:grid-cols-[1fr_1.1fr]">
            <div>
              <p className="mb-2 text-[11px] font-semibold tracking-[0.14em] text-haze uppercase">Outcome</p>
              <p className="text-sm leading-relaxed whitespace-pre-wrap text-mist">{mission.assistant_message ?? "No message yet."}</p>
              {mission.waiting_for && <p className="mt-3 text-xs text-amber">Waiting for: {mission.waiting_for.replace(/_/g, " ")}</p>}
            </div>
            <div>
              <p className="mb-3 text-[11px] font-semibold tracking-[0.14em] text-haze uppercase">Agent activity</p>
              <MissionSteps missionId={mission.mission_id} />
            </div>
          </div>
        )}
      </div>
    </li>
  );
}

export function MissionsPage() {
  const { data, isLoading, isError } = useAssistantMissions();
  const [params, setParams] = useSearchParams();
  const focus = Number(params.get("focus")) || null;
  const [filter, setFilter] = useState<"all" | MissionPhase>("all");
  const [openId, setOpenId] = useState<number | null>(focus);

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: data?.length ?? 0 };
    for (const m of data ?? []) c[missionPhase(m.status)] = (c[missionPhase(m.status)] ?? 0) + 1;
    return c;
  }, [data]);
  const shown = (data ?? []).filter((m) => filter === "all" || missionPhase(m.status) === filter);

  const toggle = (id: number) => {
    setOpenId((current) => (current === id ? null : id));
    if (focus) setParams({}, { replace: true });
  };

  return (
    <div className="mx-auto w-full max-w-4xl px-5 py-10 sm:px-8 md:pr-20 xl:pr-8">
      <PageHeader eyebrow="Missions" title="What Nexus is working on" description="Every request becomes a mission with an auditable trail of the agents and tools involved." />

      <div className="mt-8 flex gap-1.5 overflow-x-auto pb-1 animate-rise" style={{ animationDelay: "80ms" }} role="tablist" aria-label="Filter missions">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            type="button"
            role="tab"
            aria-selected={filter === f.key}
            onClick={() => setFilter(f.key)}
            className={cn(
              "flex shrink-0 cursor-pointer items-center gap-2 rounded-full px-3.5 py-1.5 text-[13px] transition-colors",
              filter === f.key ? "bg-frost text-void" : "text-haze hover:bg-glass-strong hover:text-frost",
            )}
          >
            {f.label}
            <span className={cn("text-[11px]", filter === f.key ? "text-void/60" : "text-dim")}>{counts[f.key] ?? 0}</span>
          </button>
        ))}
      </div>

      <div className="mt-6">
        {isLoading && (
          <div className="space-y-3">
            {[0, 1, 2].map((i) => (
              <div key={i} className="nx-glass space-y-3 rounded-2xl p-5">
                <GlowSkeleton className="w-2/3" />
                <GlowSkeleton className="w-1/3" />
              </div>
            ))}
          </div>
        )}
        {isError && <QuietEmpty title="Missions are unavailable right now." hint="Check that the CAMPUS AI backend is running." />}
        {data && shown.length === 0 && (
          <QuietEmpty icon={<Target />} title={filter === "all" ? "No missions yet" : "Nothing here"} hint={filter === "all" ? "Ask Nexus something — each request becomes a mission." : undefined} />
        )}
        <ul className="space-y-3">
          {shown.map((m, i) => (
            <MissionRow key={m.mission_id} mission={m} index={i} open={openId === m.mission_id} onToggle={() => toggle(m.mission_id)} />
          ))}
        </ul>
      </div>
    </div>
  );
}
