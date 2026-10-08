import { ChevronDown, Target } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { ActivityTrail, TrailPending } from "@/nexus/components/ActivityTrail";
import { GlowSkeleton, Pill, QuietEmpty, StatusDot, type Tone } from "@/nexus/components/primitives";
import { useAssistantMissions, useAutonomousMissions, useMissionSteps } from "@/nexus/hooks/useNexusData";
import {
  GUARDIAN_LIFECYCLE,
  MISSION_STATUS_LABELS,
  SPECIALIST_LIFECYCLE,
  autonomousTrail,
  guardianStage,
  missionPhase,
  missionTrail,
  progressLabel,
  resultLabel,
  specialistStage,
  type MissionPhase,
} from "@/nexus/lib/activity";
import { PageHeader } from "@/nexus/pages/PageHeader";
import type { AgentMissionStatus, AssistantMissionSummary, AutonomousMission } from "@/types/api";
import { cn } from "@/utils/cn";
import { formatDateTime, relativeTime } from "@/utils/format";

const FILTERS: { key: "all" | MissionPhase; label: string }[] = [
  { key: "all", label: "All" },
  { key: "active", label: "Running" },
  { key: "waiting", label: "Waiting" },
  { key: "done", label: "Completed" },
  { key: "failed", label: "Stopped" },
];

type Source = "all" | "requests" | "autonomous";
const SOURCES: { key: Source; label: string }[] = [
  { key: "all", label: "Everything" },
  { key: "requests", label: "Your requests" },
  { key: "autonomous", label: "Guardians" },
];

const PHASE_TONE: Record<MissionPhase, Tone> = { active: "cyan", waiting: "amber", done: "signal", failed: "rose" };

type Entry =
  | { kind: "request"; id: number; status: AgentMissionStatus; updated_at: string; mission: AssistantMissionSummary }
  | { kind: "autonomous"; id: number; status: AgentMissionStatus; updated_at: string; mission: AutonomousMission };

function Lifecycle({ stages, current, phase }: { stages: readonly string[]; current: number; phase: MissionPhase }) {
  return (
    <ol className="flex flex-wrap items-center gap-1 text-[11px]" aria-label="Mission lifecycle">
      {stages.map((stage, i) => {
        const reached = i <= current;
        const here = i === current;
        return (
          <li key={stage} className="flex items-center gap-1" aria-current={here ? "step" : undefined}>
            {i > 0 && <span className={cn("h-px w-3", reached ? "bg-line-strong" : "bg-line")} aria-hidden />}
            <span
              className={cn(
                "rounded-full px-2 py-0.5 tracking-wide",
                here ? (phase === "failed" ? "bg-rose/15 text-rose" : phase === "done" ? "bg-signal/15 text-signal" : "bg-cyan/15 text-cyan") : reached ? "text-mist" : "text-dim",
              )}
            >
              {stage}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

function KindTag({ autonomous }: { autonomous: boolean }) {
  return (
    <span className={cn("text-[10px] font-semibold tracking-[0.16em] uppercase", autonomous ? "text-violet" : "text-cyan")}>
      {autonomous ? "Autonomous guardian" : "Specialist agents"}
    </span>
  );
}

function MissionSteps({ missionId }: { missionId: number }) {
  const { data, isLoading, isError } = useMissionSteps(missionId);
  const trail = useMemo(() => (data ? missionTrail(data) : []), [data]);
  if (isLoading) return <TrailPending label="Loading agent activity" />;
  if (isError) return <p className="text-sm text-haze">Agent activity is unavailable for this mission.</p>;
  return <ActivityTrail items={trail} />;
}

function RowShell({ id, phase, open, onToggle, index, header, children }: { id: number; phase: MissionPhase; open: boolean; onToggle: () => void; index: number; header: ReactNode; children: ReactNode }) {
  const panelId = `mission-${id}`;
  return (
    <li className="animate-rise-sm" style={{ animationDelay: `${Math.min(index, 10) * 45}ms` }}>
      <div className={cn("nx-glass overflow-hidden rounded-2xl transition-colors", open && "border-line-strong")}>
        <button type="button" onClick={onToggle} aria-expanded={open} aria-controls={panelId} className="flex w-full cursor-pointer items-start gap-4 px-5 py-4 text-left">
          <span className={cn("mt-1 flex size-9 shrink-0 items-center justify-center rounded-xl ring-1 ring-inset", phase === "done" ? "bg-signal/10 ring-signal/25" : phase === "failed" ? "bg-rose/10 ring-rose/25" : phase === "waiting" ? "bg-amber/10 ring-amber/25" : "bg-cyan/10 ring-cyan/25")}>
            <StatusDot tone={PHASE_TONE[phase]} live={phase === "active" || phase === "waiting"} />
          </span>
          {header}
          <ChevronDown className={cn("mt-2 size-4 shrink-0 text-haze transition-transform", open && "rotate-180")} />
        </button>
        {open && (
          <div id={panelId} className="grid gap-6 border-t border-line px-5 py-5 md:grid-cols-[1fr_1.1fr]">
            {children}
          </div>
        )}
      </div>
    </li>
  );
}

function RequestRow({ mission, open, onToggle, index }: { mission: AssistantMissionSummary; open: boolean; onToggle: () => void; index: number }) {
  const phase = missionPhase(mission.status);
  return (
    <RowShell
      id={mission.mission_id}
      phase={phase}
      open={open}
      onToggle={onToggle}
      index={index}
      header={
        <>
          <div className="min-w-0 flex-1">
            <KindTag autonomous={false} />
            <p className={cn("text-[15px] text-frost", !open && "line-clamp-1")}>{mission.goal}</p>
            <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-haze">
              <span>Mission #{mission.mission_id}</span>
              <span>{mission.step_count} step{mission.step_count === 1 ? "" : "s"}</span>
              <span title={formatDateTime(mission.updated_at)}>Updated {relativeTime(mission.updated_at)}</span>
            </p>
            <div className="mt-2">
              <Lifecycle stages={SPECIALIST_LIFECYCLE} current={specialistStage(mission.status)} phase={phase} />
            </div>
          </div>
          <Pill tone={PHASE_TONE[phase]} className="hidden sm:inline-flex">{MISSION_STATUS_LABELS[mission.status]}</Pill>
        </>
      }
    >
      <div>
        <p className="mb-2 text-[11px] font-semibold tracking-[0.14em] text-haze uppercase">Outcome</p>
        <p className="text-sm leading-relaxed whitespace-pre-wrap text-mist">{mission.assistant_message ?? "No message yet."}</p>
        {mission.waiting_for && <p className="mt-3 text-xs text-amber">Waiting for: {mission.waiting_for.replace(/_/g, " ")}</p>}
      </div>
      <div>
        <p className="mb-3 text-[11px] font-semibold tracking-[0.14em] text-haze uppercase">Agent activity</p>
        <MissionSteps missionId={mission.mission_id} />
      </div>
    </RowShell>
  );
}

function GuardianRow({ mission, open, onToggle, index }: { mission: AutonomousMission; open: boolean; onToggle: () => void; index: number }) {
  const phase = missionPhase(mission.status);
  const progress = progressLabel(mission);
  const result = resultLabel(mission.result_code);
  const trail = useMemo(() => autonomousTrail(mission), [mission]);
  const { subject } = mission;
  return (
    <RowShell
      id={mission.mission_id}
      phase={phase}
      open={open}
      onToggle={onToggle}
      index={index}
      header={
        <>
          <div className="min-w-0 flex-1">
            <KindTag autonomous />
            <p className={cn("text-[15px] text-frost", !open && "line-clamp-1")}>
              {mission.agent_label} · {subject.title}
            </p>
            <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-haze">
              <span>Mission #{mission.mission_id}</span>
              {subject.course_code && <span>{subject.course_code}</span>}
              {progress && <span className="text-mist">{progress}</span>}
              <span title={formatDateTime(mission.updated_at)}>Updated {relativeTime(mission.updated_at)}</span>
            </p>
            <div className="mt-2">
              <Lifecycle stages={GUARDIAN_LIFECYCLE} current={guardianStage(mission)} phase={phase} />
            </div>
          </div>
          <Pill tone={PHASE_TONE[phase]} className="hidden sm:inline-flex">{MISSION_STATUS_LABELS[mission.status]}</Pill>
        </>
      }
    >
      <div className="space-y-3 text-sm text-mist">
        <p className="text-[11px] font-semibold tracking-[0.14em] text-haze uppercase">Monitoring</p>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">
          <dt className="text-haze">Agent</dt>
          <dd>{mission.agent_label}</dd>
          {subject.due_at && (
            <>
              <dt className="text-haze">{subject.type === "assignment" ? "Deadline" : subject.type === "exam" ? "Exam starts" : "Since"}</dt>
              <dd>{formatDateTime(subject.due_at)}</dd>
            </>
          )}
          {progress && (
            <>
              <dt className="text-haze">Progress</dt>
              <dd>{progress}</dd>
            </>
          )}
          {mission.next_wake_at && phase !== "done" && phase !== "failed" && (
            <>
              <dt className="text-haze">Next check</dt>
              <dd title={formatDateTime(mission.next_wake_at)}>{relativeTime(mission.next_wake_at)}</dd>
            </>
          )}
          {mission.waiting_for && (
            <>
              <dt className="text-haze">Waiting for</dt>
              <dd className="text-amber">{mission.waiting_for.replace(/_/g, " ").toLowerCase()}</dd>
            </>
          )}
          {result && (
            <>
              <dt className="text-haze">Result</dt>
              <dd>{result}</dd>
            </>
          )}
        </dl>
      </div>
      <div>
        <p className="mb-3 text-[11px] font-semibold tracking-[0.14em] text-haze uppercase">Agent activity</p>
        {trail.length ? (
          <ActivityTrail items={trail} />
        ) : (
          <p className="text-sm text-haze">{mission.progress?.scope === "self" ? "This guardian is monitoring on your behalf." : "No checks have run yet."}</p>
        )}
      </div>
    </RowShell>
  );
}

export function MissionsPage() {
  const requests = useAssistantMissions();
  const autonomous = useAutonomousMissions();
  const [params, setParams] = useSearchParams();
  const focus = Number(params.get("focus")) || null;
  const [filter, setFilter] = useState<"all" | MissionPhase>("all");
  const [source, setSource] = useState<Source>("all");
  const [openId, setOpenId] = useState<number | null>(focus);

  const entries = useMemo<Entry[]>(() => {
    const list: Entry[] = [
      ...(requests.data ?? []).map<Entry>((m) => ({ kind: "request", id: m.mission_id, status: m.status, updated_at: m.updated_at, mission: m })),
      ...(autonomous.data?.items ?? []).map<Entry>((m) => ({ kind: "autonomous", id: m.mission_id, status: m.status, updated_at: m.updated_at, mission: m })),
    ];
    return list.sort((a, b) => b.updated_at.localeCompare(a.updated_at) || b.id - a.id);
  }, [requests.data, autonomous.data]);

  const bySource = entries.filter((e) => source === "all" || (source === "requests" ? e.kind === "request" : e.kind === "autonomous"));
  const counts = useMemo(() => {
    const c: Record<string, number> = { all: bySource.length };
    for (const e of bySource) c[missionPhase(e.status)] = (c[missionPhase(e.status)] ?? 0) + 1;
    return c;
  }, [bySource]);
  const shown = bySource.filter((e) => filter === "all" || missionPhase(e.status) === filter);

  const isLoading = requests.isLoading && autonomous.isLoading;
  const isError = requests.isError && autonomous.isError;
  const loaded = Boolean(requests.data || autonomous.data);

  const toggle = (id: number) => {
    setOpenId((current) => (current === id ? null : id));
    if (focus) setParams({}, { replace: true });
  };

  return (
    <div className="mx-auto w-full max-w-4xl px-5 py-10 sm:px-8 md:pr-20 xl:pr-8">
      <PageHeader eyebrow="Missions" title="What your agents are working on" description="Your requests to Nexus, and the autonomous guardians monitoring on your behalf — each with an auditable trail." />

      <div className="mt-8 flex flex-wrap items-center gap-x-6 gap-y-3 animate-rise" style={{ animationDelay: "80ms" }}>
        <div className="flex gap-1.5 overflow-x-auto pb-1" role="tablist" aria-label="Mission source">
          {SOURCES.map((s) => (
            <button
              key={s.key}
              type="button"
              role="tab"
              aria-selected={source === s.key}
              onClick={() => setSource(s.key)}
              className={cn(
                "shrink-0 cursor-pointer rounded-full px-3.5 py-1.5 text-[13px] ring-1 ring-inset transition-colors",
                source === s.key ? "bg-glass-strong text-frost ring-line-strong" : "text-haze ring-transparent hover:text-frost",
              )}
            >
              {s.label}
            </button>
          ))}
        </div>
        <div className="flex gap-1.5 overflow-x-auto pb-1" role="tablist" aria-label="Filter missions">
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
        {!isError && (requests.isError || autonomous.isError) && (
          <p className="mb-3 text-xs text-amber">{requests.isError ? "Your requests" : "Guardian missions"} could not be loaded right now.</p>
        )}
        {loaded && shown.length === 0 && (
          <QuietEmpty icon={<Target />} title={filter === "all" && source === "all" ? "No missions yet" : "Nothing here"} hint={filter === "all" && source !== "autonomous" ? "Ask Nexus something — each request becomes a mission." : undefined} />
        )}
        <ul className="space-y-3">
          {shown.map((e, i) =>
            e.kind === "request" ? (
              <RequestRow key={`r-${e.id}`} mission={e.mission} index={i} open={openId === e.id} onToggle={() => toggle(e.id)} />
            ) : (
              <GuardianRow key={`g-${e.id}`} mission={e.mission} index={i} open={openId === e.id} onToggle={() => toggle(e.id)} />
            ),
          )}
        </ul>
      </div>
    </div>
  );
}
