import { BellRing, CalendarClock, Hand, Inbox } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "@/auth/useAuth";
import { GlowSkeleton, Pill, QuietEmpty, SectionLabel, StatusDot } from "@/nexus/components/primitives";
import { useAlerts, useAssistantMissions, useFollowUps } from "@/nexus/hooks/useNexusData";
import { PageHeader } from "@/nexus/pages/PageHeader";
import { cn } from "@/utils/cn";
import { formatDateTime, relativeTime, titleCase } from "@/utils/format";

function Block({ title, icon, children, delay, className }: { title: string; icon: ReactNode; children: ReactNode; delay: number; className?: string }) {
  return (
    <section className={cn("animate-rise", className)} style={{ animationDelay: `${delay}ms` }}>
      <SectionLabel className="mb-4">
        <span className="flex items-center gap-2 [&_svg]:size-3.5">
          {icon}
          {title}
        </span>
      </SectionLabel>
      {children}
    </section>
  );
}

function Skeletons() {
  return (
    <div className="space-y-3">
      {[0, 1, 2].map((i) => (
        <div key={i} className="nx-glass space-y-2.5 rounded-2xl p-4">
          <GlowSkeleton className="w-1/2" />
          <GlowSkeleton className="w-4/5" />
        </div>
      ))}
    </div>
  );
}

function Alerts() {
  const { user } = useAuth();
  const { data, isLoading, isError } = useAlerts(user!.role);
  if (isLoading) return <Skeletons />;
  if (isError) return <QuietEmpty title="Alerts are unavailable right now." />;
  if (!data || data.length === 0) return <QuietEmpty icon={<BellRing />} title="You're all caught up" hint="New alerts from your agents and reviewers appear here." />;
  return (
    <ul className="space-y-2.5">
      {data.map((n, i) => (
        <li key={n.id} className="nx-glass flex gap-3.5 rounded-2xl p-4 animate-rise-sm" style={{ animationDelay: `${Math.min(i, 8) * 40}ms` }}>
          <StatusDot tone={n.status === "read" ? "dim" : "rose"} live={n.status !== "read"} className="mt-1.5" />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
              <p className="text-[15px] font-medium text-frost">{n.title}</p>
              <span className="text-xs text-haze" title={formatDateTime(n.created_at)}>
                {relativeTime(n.created_at)}
              </span>
            </div>
            <p className="mt-1 text-sm leading-relaxed text-haze">{n.body}</p>
            <Pill className="mt-2.5">{titleCase(n.category)}</Pill>
          </div>
        </li>
      ))}
    </ul>
  );
}

function FollowUpList() {
  const { user } = useAuth();
  const { items, isLoading } = useFollowUps(user!.role);
  if (isLoading) return <Skeletons />;
  if (items.length === 0) return <QuietEmpty icon={<CalendarClock />} title="Nothing to follow up" hint="Upcoming exams and open requests appear here." />;
  return (
    <ul className="space-y-2.5">
      {items.map((f) => (
        <li key={f.key} className="nx-glass flex gap-3 rounded-2xl p-4">
          <span className={cn("flex size-9 shrink-0 items-center justify-center rounded-xl ring-1 ring-inset [&_svg]:size-4", f.kind === "exam" ? "bg-violet/12 text-[#c4b5fd] ring-violet/25" : "bg-amber/10 text-amber ring-amber/25")}>
            {f.kind === "exam" ? <CalendarClock /> : <Inbox />}
          </span>
          <div className="min-w-0">
            <p className="text-sm text-frost">{f.title}</p>
            <p className="mt-0.5 text-xs text-haze">{f.kind === "exam" ? `${formatDateTime(f.at)} · ${f.detail}` : f.detail}</p>
            {f.kind === "review" && <Pill tone="amber" className="mt-2">Your decision needed</Pill>}
          </div>
        </li>
      ))}
    </ul>
  );
}

function WaitingOnYou() {
  const { data } = useAssistantMissions();
  const waiting = (data ?? []).filter((m) => m.status === "waiting_human");
  if (waiting.length === 0) return <QuietEmpty icon={<Hand />} title="Nexus isn't waiting on you" />;
  return (
    <ul className="space-y-2.5">
      {waiting.map((m) => (
        <li key={m.mission_id}>
          <Link to={`/nexus/missions?focus=${m.mission_id}`} className="nx-glass block rounded-2xl p-4 transition-colors hover:border-line-strong">
            <p className="line-clamp-2 text-sm text-frost">{m.assistant_message ?? m.goal}</p>
            <p className="mt-1 text-xs text-haze">Mission #{m.mission_id} · {relativeTime(m.updated_at)}</p>
          </Link>
        </li>
      ))}
    </ul>
  );
}

export function NotificationsPage() {
  return (
    <div className="mx-auto w-full max-w-6xl px-5 py-10 sm:px-8 md:pr-20 xl:pr-8">
      <PageHeader eyebrow="Alerts & follow-ups" title="What needs your attention" description="Alerts from your agents and reviewers, what's coming up, and anything Nexus is waiting on you for." />
      <div className="mt-10 grid gap-10 lg:grid-cols-[1.4fr_1fr]">
        <Block title="Alerts" icon={<BellRing className="text-rose" />} delay={80}>
          <Alerts />
        </Block>
        <div className="space-y-10">
          <Block title="Waiting on you" icon={<Hand className="text-amber" />} delay={140}>
            <WaitingOnYou />
          </Block>
          <Block title="Upcoming follow-ups" icon={<CalendarClock className="text-violet" />} delay={200}>
            <FollowUpList />
          </Block>
        </div>
      </div>
    </div>
  );
}
