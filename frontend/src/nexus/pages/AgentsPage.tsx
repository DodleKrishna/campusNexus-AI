import { ArrowUpRight, Sparkles } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "@/auth/useAuth";
import { NxButton, Pill, type Tone } from "@/nexus/components/primitives";
import { AGENT_NETWORK, KIND_LABELS, type AgentKind, type AgentProfile } from "@/nexus/lib/agents";
import { PageHeader } from "@/nexus/pages/PageHeader";
import { useAssistant } from "@/nexus/state/useAssistant";
import type { Role } from "@/types/api";
import { cn } from "@/utils/cn";

const KIND_TONE: Record<AgentKind, Tone> = { orchestrator: "cyan", specialist: "violet", guardian: "signal", workflow: "amber", safeguard: "dim" };
const KIND_ICON_BG: Record<AgentKind, string> = {
  orchestrator: "from-cyan/30 to-electric/20 text-cyan ring-cyan/30",
  specialist: "from-violet/25 to-electric/10 text-[#c4b5fd] ring-violet/30",
  guardian: "from-signal/20 to-cyan/10 text-signal ring-signal/25",
  workflow: "from-amber/20 to-rose/10 text-amber ring-amber/25",
  safeguard: "from-white/10 to-white/0 text-mist ring-line-strong",
};

/** Node positions for the network diagram (viewBox 800 × 360), around Nexus at the centre. */
const RING: { key: string; x: number; y: number; label: string; kind: AgentKind }[] = [
  { key: "academic", x: 140, y: 92, label: "Academic", kind: "specialist" },
  { key: "career", x: 400, y: 58, label: "Career", kind: "specialist" },
  { key: "events", x: 660, y: 92, label: "Events", kind: "specialist" },
  { key: "campus_services", x: 730, y: 210, label: "Campus Services", kind: "specialist" },
  { key: "assignment_guardian", x: 590, y: 318, label: "Assignment Guardian", kind: "guardian" },
  { key: "exam_guardian", x: 400, y: 330, label: "Exam Guardian", kind: "guardian" },
  { key: "attendance_guardian", x: 210, y: 318, label: "Attendance Guardian", kind: "guardian" },
  { key: "knowledge", x: 70, y: 210, label: "Knowledge", kind: "specialist" },
];
const NODE_COLOR: Record<AgentKind, string> = { orchestrator: "#22d3ee", specialist: "#8b5cf6", guardian: "#34d399", workflow: "#fbbf24", safeguard: "#b9c3dd" };

function NetworkDiagram() {
  const cx = 400;
  const cy = 190;
  return (
    <div className="nx-glass relative mt-8 overflow-hidden rounded-3xl animate-rise" style={{ animationDelay: "80ms" }}>
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_50%_60%_at_50%_52%,rgb(34_211_238/0.12),transparent_70%)]" aria-hidden />
      <svg viewBox="0 0 800 370" className="relative w-full" role="img" aria-label="Nexus at the centre, connected to the specialist agents and guardians">
        <defs>
          <radialGradient id="nx-core" cx="0.5" cy="0.5" r="0.5">
            <stop offset="0" stopColor="#e0f7ff" />
            <stop offset="0.45" stopColor="#22d3ee" />
            <stop offset="1" stopColor="#3b82f6" stopOpacity="0.2" />
          </radialGradient>
        </defs>
        <ellipse cx={cx} cy={cy} rx="330" ry="150" fill="none" stroke="rgb(148 163 255 / 0.08)" strokeDasharray="2 6" />
        {RING.map((n, i) => (
          <g key={n.key}>
            <line x1={cx} y1={cy} x2={n.x} y2={n.y} stroke={NODE_COLOR[n.kind]} strokeOpacity="0.16" strokeWidth="1" />
            <line
              x1={cx} y1={cy} x2={n.x} y2={n.y} stroke={NODE_COLOR[n.kind]} strokeOpacity="0.7" strokeWidth="1.4" strokeDasharray="3 25" strokeLinecap="round"
              className="animate-flow" style={{ animationDelay: `${i * 0.22}s`, animationDuration: `${1.6 + (i % 3) * 0.4}s` }}
            />
          </g>
        ))}
        {RING.map((n, i) => (
          <g key={`${n.key}-node`} className="animate-float" style={{ animationDelay: `${i * 0.5}s`, transformBox: "fill-box" }}>
            <circle cx={n.x} cy={n.y} r="16" fill="#0b1226" stroke={NODE_COLOR[n.kind]} strokeOpacity="0.55" />
            <circle cx={n.x} cy={n.y} r="5" fill={NODE_COLOR[n.kind]} />
            <text x={n.x} y={n.y + (n.y > cy ? 34 : -26)} textAnchor="middle" fill="#b9c3dd" fontSize="12.5" fontFamily="Inter, sans-serif">
              {n.label}
            </text>
          </g>
        ))}
        <circle cx={cx} cy={cy} r="64" fill="url(#nx-core)" opacity="0.22" />
        <circle cx={cx} cy={cy} r="38" fill="#0b1226" stroke="#22d3ee" strokeOpacity="0.6" />
        <circle cx={cx} cy={cy} r="20" fill="url(#nx-core)" />
        <text x={cx} y={cy + 62} textAnchor="middle" fill="#eef2ff" fontSize="14" fontWeight="600" fontFamily="'Plus Jakarta Sans', Inter, sans-serif">
          Nexus
        </text>
      </svg>
    </div>
  );
}

function AgentCard({ agent, role, onAsk, index }: { agent: AgentProfile; role: Role; onAsk: (prompt: string) => void; index: number }) {
  const Icon = agent.icon;
  const canAsk = agent.prompt && agent.promptRoles?.includes(role);
  const classic = agent.classic?.[role];
  return (
    <li className="nx-glass group flex flex-col rounded-2xl p-5 transition-all duration-300 hover:-translate-y-0.5 hover:border-line-strong animate-rise-sm" style={{ animationDelay: `${120 + index * 40}ms` }}>
      <div className="flex items-start justify-between gap-3">
        <span className={cn("flex size-11 items-center justify-center rounded-xl bg-gradient-to-br ring-1 ring-inset", KIND_ICON_BG[agent.kind])}>
          <Icon className="size-5" />
        </span>
        <Pill tone={KIND_TONE[agent.kind]}>{KIND_LABELS[agent.kind]}</Pill>
      </div>
      <h3 className="mt-4 font-display text-base font-semibold text-frost">{agent.name}</h3>
      <p className="mt-1.5 flex-1 text-sm leading-relaxed text-haze">{agent.summary}</p>
      <ul className="mt-4 flex flex-wrap gap-1.5">
        {agent.capabilities.map((c) => (
          <li key={c} className="rounded-md bg-white/[0.04] px-2 py-0.5 text-[11px] text-mist ring-1 ring-line ring-inset">
            {c}
          </li>
        ))}
      </ul>
      {(canAsk || classic) && (
        <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-line pt-4">
          {canAsk && (
            <NxButton size="sm" onClick={() => onAsk(agent.prompt!)}>
              <Sparkles className="text-cyan" /> Ask via Nexus
            </NxButton>
          )}
          {classic && (
            <Link to={classic} className="inline-flex items-center gap-1 text-xs text-haze transition-colors hover:text-frost">
              Open workspace <ArrowUpRight className="size-3.5" />
            </Link>
          )}
        </div>
      )}
    </li>
  );
}

export function AgentsPage() {
  const { user } = useAuth();
  const { setDraft } = useAssistant();
  const navigate = useNavigate();
  if (!user) return null;
  const ask = (prompt: string) => {
    setDraft(prompt);
    navigate("/nexus");
  };
  return (
    <div className="mx-auto w-full max-w-6xl px-5 py-10 sm:px-8 md:pr-20 xl:pr-8">
      <PageHeader eyebrow="Agents" title="One intelligence, many agents" description="Nexus plans; specialists answer with evidence; guardians watch over time; deterministic safeguards keep every action correct and approved." />
      <NetworkDiagram />
      <ul className="mt-8 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {AGENT_NETWORK.map((agent, i) => (
          <AgentCard key={agent.key} agent={agent} role={user.role} onAsk={ask} index={i} />
        ))}
      </ul>
    </div>
  );
}
