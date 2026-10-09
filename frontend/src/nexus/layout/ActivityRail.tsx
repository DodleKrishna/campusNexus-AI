import { Activity, BellRing, Bot, CalendarClock, Inbox, Radio, Target } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "@/auth/useAuth";
import { useHealth } from "@/hooks/useStudentData";
import { ActivityTrail } from "@/nexus/components/ActivityTrail";
import { GlowSkeleton, Pill, QuietEmpty, SectionLabel, StatusDot } from "@/nexus/components/primitives";
import { useActiveMissions, useAlerts, useAutonomousMissions, useFollowUps } from "@/nexus/hooks/useNexusData";
import { MISSION_STATUS_LABELS, autonomousStatus, missionPhase } from "@/nexus/lib/activity";
import { useAssistant } from "@/nexus/state/useAssistant";
import { cn } from "@/utils/cn";
import { formatDateTime, relativeTime } from "@/utils/format";

function Section({ title, icon, action, children, delay = 0 }: { title: string; icon: ReactNode; action?: ReactNode; children: ReactNode; delay?: number }) {
  return (
    <section className="animate-rise-sm" style={{ animationDelay: `${delay}ms` }}>
      <SectionLabel
        action={action}
        className="mb-3"
      >
        <span className="flex items-center gap-2 [&_svg]:size-3.5 [&_svg]:text-dim">
          {icon}
          {title}
        </span>
      </SectionLabel>
      {children}
    </section>
  );
}

function Loading() {
  return (
    <div className="space-y-2.5 py-1">
      <GlowSkeleton className="w-4/5" />
      <GlowSkeleton className="w-3/5" />
    </div>
  );
}

/**
 * Whether replies come from a live model; mock mode is never presented as live. When the deployment reports it is
 * offline (edge mode), that wins: campus answers then come from local records and the local model.
 */
export function AiModeBadge() {
  const { brain } = useAssistant();
  const health = useHealth();
  if (health.data?.connectivity === "offline") {
    return (
      <span title="No internet: answers come from local campus records and the on-device model.">
        <Pill tone="violet">
          <StatusDot tone="violet" live /> Edge AI · Offline
        </Pill>
      </span>
    );
  }
  const live = brain ? brain.live : health.data?.llm.live;
  if (live === undefined) return null;
  return live ? (
    <Pill tone="signal">
      <StatusDot tone="signal" live /> Live AI
    </Pill>
  ) : (
    <Pill tone="amber">
      <StatusDot tone="amber" /> Offline mock
    </Pill>
  );
}

function ActiveMissions() {
  const { active, isLoading, isError } = useActiveMissions();
  if (isLoading) return <Loading />;
  if (isError) return <QuietEmpty title="Missions are unavailable right now." />;
  if (active.length === 0) return <QuietEmpty icon={<Target />} title="No active missions" hint="Missions you start with Nexus, and guardians watching for you, show up here." />;
  return (
    <ul className="space-y-2">
      {active.slice(0, 4).map((m) => (
        <li key={m.id}>
          <Link to={`/nexus/missions?focus=${m.id}`} className="block rounded-xl border border-line bg-white/[0.02] px-3 py-2.5 transition-colors hover:border-line-strong hover:bg-white/[0.04]">
            <div className="flex items-center justify-between gap-2">
              <span className={cn("text-[11px]", m.agent ? "text-violet" : "text-haze")}>{m.agent ?? `Mission #${m.id}`}</span>
              <Pill tone={missionPhase(m.status) === "waiting" ? "amber" : "cyan"}>
                <StatusDot tone={missionPhase(m.status) === "waiting" ? "amber" : "cyan"} live />
                {MISSION_STATUS_LABELS[m.status]}
              </Pill>
            </div>
            <p className="mt-1.5 line-clamp-2 text-[13px] text-mist">{m.title}</p>
            {m.detail && <p className="mt-1 text-[11px] text-haze">{m.detail}</p>}
          </Link>
        </li>
      ))}
    </ul>
  );
}

/** The Guardians and the Communication Agent working on their own, refreshed every few seconds. */
function AutonomousAgents() {
  const { data, isLoading, isError } = useAutonomousMissions();
  if (isLoading) return <Loading />;
  if (isError) return <QuietEmpty title="Autonomous agents are unavailable right now." />;
  const items = (data?.items ?? []).slice(0, 5);
  if (items.length === 0) return <QuietEmpty icon={<Bot />} title="No guardians yet" hint="Guardians start when exams, assignments or absences need watching." />;
  return (
    <ul className="space-y-2" aria-live="polite">
      {items.map((m) => {
        const status = autonomousStatus(m);
        return (
          <li key={m.mission_id} className="rounded-xl border border-line bg-white/[0.02] px-3 py-2.5">
            <div className="flex items-center justify-between gap-2">
              <span className="text-[11px] text-violet">{m.agent_label}</span>
              <Pill tone={status.tone}>
                <StatusDot tone={status.tone} live={status.tone === "cyan" || status.tone === "amber"} />
                {status.headline}
              </Pill>
            </div>
            <p className="mt-1.5 line-clamp-1 text-[13px] text-mist">{m.subject.title}</p>
            {status.detail && <p className="mt-0.5 text-[11px] text-haze">{status.detail}</p>}
          </li>
        );
      })}
    </ul>
  );
}

function AgentActivity() {
  const { activity } = useAssistant();
  if (activity.length === 0) return <QuietEmpty icon={<Activity />} title="Quiet for now" hint="Agent steps appear here as Nexus works." />;
  return <ActivityTrail items={activity.slice(0, 6)} compact />;
}

function RecentAlerts() {
  const { user } = useAuth();
  const { data, isLoading, isError } = useAlerts(user!.role);
  if (isLoading) return <Loading />;
  if (isError) return <QuietEmpty title="Alerts are unavailable right now." />;
  if (!data || data.length === 0) return <QuietEmpty icon={<BellRing />} title="You're all caught up" />;
  return (
    <ul className="space-y-1">
      {data.slice(0, 3).map((n) => (
        <li key={n.id} className="flex gap-2.5 rounded-xl px-1 py-1.5">
          <StatusDot tone={n.status === "read" ? "dim" : "rose"} className="mt-1.5" />
          <div className="min-w-0">
            <p className="truncate text-[13px] text-mist">{n.title}</p>
            <p className="text-[11px] text-haze">{relativeTime(n.created_at)}</p>
          </div>
        </li>
      ))}
    </ul>
  );
}

function FollowUps() {
  const { user } = useAuth();
  const { items, isLoading } = useFollowUps(user!.role);
  if (isLoading) return <Loading />;
  if (items.length === 0) return <QuietEmpty icon={<CalendarClock />} title="Nothing scheduled" hint="Exams and open requests appear here." />;
  return (
    <ul className="space-y-1">
      {items.slice(0, 4).map((f) => (
        <li key={f.key} className="flex gap-2.5 rounded-xl px-1 py-1.5">
          <span className={cn("mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-lg ring-1 ring-inset [&_svg]:size-3.5", f.kind === "exam" ? "bg-violet/12 text-[#c4b5fd] ring-violet/25" : "bg-amber/10 text-amber ring-amber/25")}>
            {f.kind === "exam" ? <CalendarClock /> : <Inbox />}
          </span>
          <div className="min-w-0">
            <p className="truncate text-[13px] text-mist">{f.title}</p>
            <p className="truncate text-[11px] text-haze">
              {f.kind === "exam" ? formatDateTime(f.at) : f.detail}
            </p>
          </div>
        </li>
      ))}
    </ul>
  );
}

/** The right-hand live context: missions, agent activity, alerts and follow-ups. */
export function ActivityRail({ className }: { className?: string }) {
  return (
    <aside aria-label="Live activity" className={cn("nx-scroll flex flex-col gap-7 overflow-y-auto px-5 py-6", className)}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Radio className="size-4 text-cyan" />
          <span className="font-display text-sm font-semibold text-frost">Live context</span>
        </div>
        <AiModeBadge />
      </div>
      <Section title="Active missions" icon={<Target />} delay={60} action={<Link to="/nexus/missions" className="text-[11px] text-haze hover:text-cyan">All</Link>}>
        <ActiveMissions />
      </Section>
      <Section title="Autonomous agents" icon={<Bot />} delay={90}>
        <AutonomousAgents />
      </Section>
      <Section title="Agent activity" icon={<Activity />} delay={120}>
        <AgentActivity />
      </Section>
      <Section title="Recent alerts" icon={<BellRing />} delay={180} action={<Link to="/nexus/notifications" className="text-[11px] text-haze hover:text-cyan">All</Link>}>
        <RecentAlerts />
      </Section>
      <Section title="Upcoming follow-ups" icon={<CalendarClock />} delay={240}>
        <FollowUps />
      </Section>
    </aside>
  );
}
