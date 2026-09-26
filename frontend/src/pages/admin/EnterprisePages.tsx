import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Activity, ArrowDown, ArrowRight, BadgeCheck, BookOpen, Bot, Boxes, Building2, Check, ChevronDown, ChevronRight, CircleDollarSign,
  ClipboardCheck, Clock3, Cpu, Database, FileSpreadsheet, Gauge, GitBranch, Globe, Hourglass, Inbox, Layers, Mail, Minus,
  PiggyBank, Plug, Radar, RefreshCw, Scale, ShieldAlert, ShieldCheck, Sparkles, Timer, TrendingUp, Users, Workflow, Zap,
} from "lucide-react";
import { Fragment, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, queryKeys } from "@/api/endpoints";
import { useAuth } from "@/auth/useAuth";
import { Badge, StatusBadge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { CellTitle, DataTable } from "@/components/ui/data-table";
import { Input, Label } from "@/components/ui/input";
import { MetricCard, MetricSkeletons } from "@/components/ui/metric-card";
import { SkeletonTable } from "@/components/ui/skeleton";
import { EmptyState, ErrorState, Notice } from "@/components/ui/states";
import { AgentRuns, DeployedAgents } from "@/features/admin/AgentProducts";
import { ControlTower, LevelBadge } from "@/features/admin/IntelligenceOps";
import {
  useAdminAIOperations, useAdminCommandCenter, useAdminConnectors, useAdminDeployments, useAdminKnowledge, useAdminMonitors,
  useAdminWorkflows,
} from "@/hooks/useAdminData";
import { PageTitle } from "@/pages/PageTitle";
import type { AgentRunRow, Connector, Monitor, WorkflowCard } from "@/types/api";
import { cn } from "@/utils/cn";
import { formatDateTime, relativeTime, titleCase } from "@/utils/format";

// ------------------------------------------------------------------------------------------------------------------
// Shared building blocks
// ------------------------------------------------------------------------------------------------------------------

function Section({ title, description, action, children, className }: { title: string; description?: string; action?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <Card className={cn("overflow-hidden", className)}>
      <CardHeader title={title} description={description} action={action} />
      <div className="border-t border-border">{children}</div>
    </Card>
  );
}

function Loading({ rows = 4 }: { rows?: number }) {
  return <SkeletonTable rows={rows} />;
}

const KPI_ICON: Record<string, ReactNode> = {
  agents: <Bot />, workflows: <Workflow />, approvals: <ShieldCheck />, sla: <Timer />, no_ai: <Zap />, spend: <CircleDollarSign />,
};
const HEALTH_ICON: Record<string, ReactNode> = {
  attendance: <ClipboardCheck />, requests: <Inbox />, approvals: <ShieldCheck />, complaints: <ShieldAlert />, deadlines: <Hourglass />,
};

/** The CampusNexus enterprise stack: an intelligent layer above existing systems (never a rip-and-replace). */
const STACK: { title: string; icon: ReactNode; items?: string[]; tone?: string }[] = [
  { title: "Existing college systems", icon: <Building2 />, items: ["ERP", "SIS", "LMS", "Attendance", "Placement", "CSV", "REST API"], tone: "bg-surface-muted" },
  { title: "Connector Hub", icon: <Plug /> },
  { title: "Unified campus context", icon: <Layers /> },
  { title: "Workflow + rules engine", icon: <GitBranch />, items: ["Deterministic rules", "Eligibility", "Routing", "SLA", "Escalation"] },
  { title: "Adaptive intelligence router", icon: <Cpu />, items: ["No AI · deterministic", "Light · GPT-OSS 20B", "Advanced · GPT-OSS 120B"] },
  { title: "Specialized agents", icon: <Bot /> },
  { title: "Controlled actions", icon: <Scale /> },
  { title: "Human approval when required", icon: <ShieldCheck /> },
  { title: "System write-back / notification", icon: <Mail /> },
  { title: "Audit + Control Tower", icon: <Activity />, tone: "bg-primary-soft" },
];

export function EnterpriseStack({ compact = false }: { compact?: boolean }) {
  return (
    <ol className={cn("grid gap-2", compact ? "grid-cols-1 md:grid-cols-2" : "grid-cols-1")}>
      {STACK.map((layer, i) => (
        <li key={layer.title} className="relative">
          <div className={cn("flex h-full items-start gap-3 rounded-lg border border-border px-3.5 py-3", layer.tone ?? "bg-surface")}>
            <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary [&_svg]:size-4">{layer.icon}</span>
            <div className="min-w-0">
              <p className="text-[13px] font-semibold text-ink">
                <span className="mr-1.5 text-xs font-medium text-subtle tabular-nums">{String(i + 1).padStart(2, "0")}</span>
                {layer.title}
              </p>
              {layer.items && (
                <div className="mt-1.5 flex flex-wrap gap-1">
                  {layer.items.map((item) => (
                    <Badge key={item} className="font-normal">{item}</Badge>
                  ))}
                </div>
              )}
            </div>
          </div>
          {!compact && i < STACK.length - 1 && <ArrowDown aria-hidden className="mx-auto my-0.5 size-3.5 text-subtle" />}
        </li>
      ))}
    </ol>
  );
}

const GOVERNANCE = [
  { label: "AI recommends", icon: <Sparkles /> },
  { label: "Rules verify", icon: <BadgeCheck /> },
  { label: "Humans approve", icon: <ShieldCheck /> },
  { label: "System executes", icon: <Zap /> },
  { label: "Audit records", icon: <Activity /> },
];

export function GovernanceStrip() {
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-card border border-border bg-surface px-4 py-3">
      <span className="mr-1 text-xs font-semibold tracking-wide text-muted uppercase">AI never decides alone</span>
      {GOVERNANCE.map((g, i) => (
        <Fragment key={g.label}>
          <span className="inline-flex items-center gap-1.5 rounded-md bg-surface-muted px-2.5 py-1 text-[13px] font-medium text-ink [&_svg]:size-3.5 [&_svg]:text-primary">
            {g.icon}
            {g.label}
          </span>
          {i < GOVERNANCE.length - 1 && <ArrowRight aria-hidden className="size-3.5 text-subtle" />}
        </Fragment>
      ))}
    </div>
  );
}

function Pill({ children, tone = "neutral" }: { children: ReactNode; tone?: BadgeTone }) {
  return <Badge tone={tone}>{children}</Badge>;
}

// ------------------------------------------------------------------------------------------------------------------
// Command Center
// ------------------------------------------------------------------------------------------------------------------

export function AdminCommandCenterPage() {
  const { user } = useAuth();
  const { data, isLoading, isError, error, refetch } = useAdminCommandCenter();
  return (
    <div className="space-y-6">
      <PageTitle
        title="CampusNexus Command Center"
        description="AI workforce and workflow automation across your institution."
        action={
          <>
            {user?.organization && <Pill tone="primary">{user.organization.name}</Pill>}
            <Button variant="outline" size="sm" onClick={() => void refetch()}>
              <RefreshCw /> Refresh
            </Button>
          </>
        }
      />
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {isLoading && <MetricSkeletons count={6} />}
      {data && (
        <>
          <div className="grid grid-cols-2 gap-3 sm:gap-4 md:grid-cols-3 2xl:grid-cols-6">
            {data.kpis.map((k) => (
              <MetricCard key={k.key} label={k.label} value={k.value} context={k.note} icon={KPI_ICON[k.key]}
                          tone={k.key === "sla" && k.value !== "0" ? "warning" : "default"} />
            ))}
          </div>

          <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
            <Section title="Operational health" description="Live counts from your institution's records." className="xl:col-span-1">
              <ul className="divide-y divide-border">
                {data.health.map((h) => (
                  <li key={h.key}>
                    <Link to={h.link} className="flex items-center gap-3 px-5 py-3 transition-colors hover:bg-surface-muted">
                      <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-surface-muted text-muted [&_svg]:size-4">{HEALTH_ICON[h.key]}</span>
                      <span className="min-w-0 flex-1">
                        <span className="block text-sm font-medium text-ink">{h.label}</span>
                        <span className="block truncate text-xs text-muted">{h.note}</span>
                      </span>
                      <span className={cn("text-lg font-semibold tabular-nums", h.value ? "text-ink" : "text-subtle")}>{h.value}</span>
                      <ChevronRight className="size-4 text-subtle" aria-hidden />
                    </Link>
                  </li>
                ))}
              </ul>
            </Section>

            <Section title="AI workforce" description="Deployed product agents and how they are running."
                     action={<Link to="/admin/agent-catalog" className="text-[13px] font-medium text-primary hover:underline">Manage</Link>}
                     className="xl:col-span-2">
              {data.workforce.length === 0 ? (
                <EmptyState compact title="No agents deployed" description="Deploy agents from the Agent Catalog." />
              ) : (
                <div className="overflow-x-auto">
                  <DataTable columns={["Agent", "Status", "Intelligence", "Runs", "Success", "Est. cost"]} caption="AI workforce">
                    {data.workforce.map((d) => (
                      <tr key={d.id}>
                        <td><CellTitle title={d.display_name} subtitle={d.requires_approval ? "Human approval on actions" : "Read-only answers"} /></td>
                        <td><StatusBadge label={d.status === "active" ? "Active" : "Paused"} tone={d.status === "active" ? "success" : "warning"} /></td>
                        <td><LevelBadge level={d.intelligence_level} /></td>
                        <td className="tabular-nums">{d.runs}</td>
                        <td className="tabular-nums">{d.success_rate_percent == null ? "—" : `${d.success_rate_percent}%`}</td>
                        <td className="tabular-nums">{d.estimated_cost_usd == null ? (d.runs ? "Unavailable" : "—") : `$${d.estimated_cost_usd.toFixed(4)}`}</td>
                      </tr>
                    ))}
                  </DataTable>
                </div>
              )}
            </Section>
          </div>

          <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
            <Section title="Recent automation activity" description="From the audit trail." className="xl:col-span-1"
                     action={<Link to="/admin/audit" className="text-[13px] font-medium text-primary hover:underline">Audit log</Link>}>
              {data.activity.length === 0 ? (
                <EmptyState compact title="No activity yet" />
              ) : (
                <ol className="space-y-0 px-5 py-3">
                  {data.activity.map((a, i) => (
                    <li key={`${a.timestamp}-${i}`} className="relative flex gap-3 pb-3 last:pb-0">
                      <span aria-hidden className="mt-1.5 size-2 shrink-0 rounded-full bg-primary ring-4 ring-primary/10" />
                      <div className="min-w-0">
                        <p className="line-clamp-2 text-[13px] text-ink">{a.outcome}</p>
                        <p className="text-xs text-muted">{titleCase(a.action)} · {relativeTime(a.timestamp)}</p>
                      </div>
                    </li>
                  ))}
                </ol>
              )}
            </Section>
            <Section title="How CampusNexus works" className="xl:col-span-2"
                     description="CampusNexus does not replace the college ERP. It is the intelligent automation and agent layer above existing systems.">
              <div className="p-4">
                <EnterpriseStack compact />
              </div>
            </Section>
          </div>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------------------------
// Workflows
// ------------------------------------------------------------------------------------------------------------------

const SLA_LEGEND: { label: string; tone: BadgeTone; note: string }[] = [
  { label: "On Track", tone: "success", note: "< 24 h" },
  { label: "Due Soon", tone: "warning", note: "24–48 h" },
  { label: "SLA Risk", tone: "danger", note: "> 48 h" },
  { label: "Escalated", tone: "danger", note: "routed up the chain" },
];

function WorkflowTile({ w }: { w: WorkflowCard }) {
  return (
    <Card className="flex h-full flex-col gap-4 p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-semibold text-ink">{w.name}</p>
          <p className="mt-0.5 text-[13px] text-muted">Trigger: {w.trigger}</p>
        </div>
        <StatusBadge label={w.status === "active" ? "Active" : "Partial"} tone={w.status === "active" ? "success" : "caution"} />
      </div>
      <ol className="flex flex-wrap items-center gap-1.5">
        {w.chain.map((step, i) => (
          <Fragment key={step}>
            <li className="rounded-md border border-border bg-surface-muted px-2 py-1 text-xs text-ink">{step}</li>
            {i < w.chain.length - 1 && <ArrowRight aria-hidden className="size-3 text-subtle" />}
          </Fragment>
        ))}
      </ol>
      <dl className="mt-auto grid grid-cols-3 gap-2 border-t border-border pt-3 text-center">
        {[
          { label: "Processed", value: w.processed },
          { label: "Pending", value: w.pending },
          { label: "Escalations", value: w.escalations },
        ].map((m) => (
          <div key={m.label}>
            <dt className="text-xs text-muted">{m.label}</dt>
            <dd className={cn("text-lg font-semibold tabular-nums", m.label === "Escalations" && m.value ? "text-danger-strong" : "text-ink")}>{m.value}</dd>
          </div>
        ))}
      </dl>
      <p className="flex items-center gap-1.5 text-xs text-muted">
        <Clock3 className="size-3.5" aria-hidden /> SLA: {w.sla}
      </p>
      {w.note && <p className="text-xs text-caution-strong">{w.note}</p>}
    </Card>
  );
}

export function AdminWorkflowsPage() {
  const { data, isLoading, isError, error, refetch } = useAdminWorkflows();
  return (
    <div className="space-y-6">
      <PageTitle title="Workflows" description="Institutional processes automated end to end: deterministic checks, routed reviewers, SLAs, notifications and a full audit trail." />
      <GovernanceStrip />
      <Card className="flex flex-wrap items-center gap-2 px-4 py-3">
        <span className="mr-1 text-xs font-semibold tracking-wide text-muted uppercase">Request SLA</span>
        {SLA_LEGEND.map((s) => (
          <span key={s.label} className="inline-flex items-center gap-1.5 text-xs text-muted">
            <StatusBadge label={s.label} tone={s.tone} /> {s.note}
          </span>
        ))}
        <span className="ml-auto text-xs text-muted">Routing escalation is live · SLA chips are a demo indicator · scheduled auto-escalation is on the roadmap</span>
      </Card>
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {isLoading && <Loading />}
      {data && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 2xl:grid-cols-3">
          {data.map((w) => (
            <WorkflowTile key={w.key} w={w} />
          ))}
        </div>
      )}
      <Notice tone="info">
        Proactive checks run the same deterministic rules without waiting for a chat message: see <Link to="/admin/monitors" className="font-medium text-primary hover:underline">Monitors</Link>.
      </Notice>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------------------------
// Proactive monitors
// ------------------------------------------------------------------------------------------------------------------

const SEVERITY: Record<string, BadgeTone> = { critical: "danger", warning: "warning", info: "info" };

function MonitorCard({ m }: { m: Monitor }) {
  const client = useQueryClient();
  const run = useMutation({
    mutationFn: () => api.adminRunMonitor(m.key),
    onSuccess: () => {
      for (const key of ["monitors", "command-center"]) void client.invalidateQueries({ queryKey: queryKeys.admin(key) });
    },
  });
  return (
    <Card className="flex h-full flex-col">
      <div className="flex items-start justify-between gap-3 p-5 pb-3">
        <div className="min-w-0">
          <p className="flex items-center gap-2 font-semibold text-ink">
            <Radar className="size-4 text-primary" aria-hidden /> {m.name}
          </p>
          <p className="mt-1 text-[13px] text-muted">{m.description}</p>
        </div>
        <StatusBadge label={`${m.findings_count} finding${m.findings_count === 1 ? "" : "s"}`} tone={m.findings_count ? "warning" : "success"} />
      </div>
      <ul className="flex-1 divide-y divide-border border-t border-border">
        {m.findings.length === 0 ? (
          <li className="px-5 py-4 text-sm text-muted">Nothing needs attention.</li>
        ) : (
          m.findings.map((f, i) => (
            <li key={`${f.title}-${i}`} className="flex items-start justify-between gap-3 px-5 py-2.5">
              <div className="min-w-0">
                <p className="truncate text-[13px] font-medium text-ink">{f.title}</p>
                <p className="truncate text-xs text-muted">{f.detail}</p>
              </div>
              <Badge tone={SEVERITY[f.severity]}>{titleCase(f.severity)}</Badge>
            </li>
          ))
        )}
      </ul>
      <div className="flex items-center justify-between gap-3 border-t border-border px-5 py-3">
        <span className="text-xs text-muted">{m.last_checked ? `Last checked ${relativeTime(run.data?.last_checked ?? m.last_checked)}` : "Not run yet"} · {m.actions}</span>
        <Button size="sm" variant="outline" disabled={run.isPending} onClick={() => run.mutate()}>
          <RefreshCw className={cn(run.isPending && "animate-spin")} /> Run check
        </Button>
      </div>
    </Card>
  );
}

export function AdminMonitorsPage() {
  const { data, isLoading, isError, error, refetch } = useAdminMonitors();
  return (
    <div className="space-y-6">
      <PageTitle title="Proactive monitors" description="Deterministic checks that surface risks before anyone asks. Every run is recorded in the audit trail." />
      <Notice tone="info">Checks run on demand here; a production scheduler (cron) is on the roadmap. Findings come only from your institution's records.</Notice>
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {isLoading && <Loading />}
      {data && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 2xl:grid-cols-3">
          {data.map((m) => (
            <MonitorCard key={m.key} m={m} />
          ))}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------------------------
// Connectors
// ------------------------------------------------------------------------------------------------------------------

const CATEGORY_ICON: Record<string, ReactNode> = {
  Database: <Database />, "REST API": <Globe />, "CSV / Excel": <FileSpreadsheet />, Attendance: <ClipboardCheck />,
  Placement: <TrendingUp />, Communication: <Mail />,
};
const CONNECTOR_STATE: Record<Connector["status"], { label: string; tone: BadgeTone }> = {
  connected: { label: "Connected", tone: "success" },
  available: { label: "Available", tone: "info" },
  coming_next: { label: "Coming next", tone: "neutral" },
};
const ADAPTER_FLOW = ["External system", "Connector adapter", "Normalized campus data", "Context Service", "Rules + agents + workflows", "Controlled write-back"];

export function AdminConnectorsPage() {
  const { data, isLoading, isError, error, refetch } = useAdminConnectors();
  return (
    <div className="space-y-6">
      <PageTitle title="Connector Hub" description="Connect CampusNexus to existing institutional systems instead of replacing them." />
      <Card className="p-5">
        <p className="text-sm font-semibold text-ink">No rip-and-replace</p>
        <p className="mt-1 text-sm text-muted">Your ERP, SIS and LMS stay the systems of record. Adapters normalize their data into the campus context; agents act only through controlled, approved write-back.</p>
        <ol className="mt-4 flex flex-wrap items-center gap-2">
          {ADAPTER_FLOW.map((step, i) => (
            <Fragment key={step}>
              <li className={cn("rounded-md border px-2.5 py-1.5 text-[13px]", i === 0 || i === ADAPTER_FLOW.length - 1 ? "border-primary/30 bg-primary-soft text-primary-hover" : "border-border bg-surface-muted text-ink")}>{step}</li>
              {i < ADAPTER_FLOW.length - 1 && <ArrowRight aria-hidden className="size-3.5 text-subtle" />}
            </Fragment>
          ))}
        </ol>
      </Card>
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {isLoading && <Loading />}
      {data && (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
          {data.map((c) => (
            <Card key={c.key} className={cn("flex h-full flex-col gap-3 p-5", c.status === "coming_next" && "bg-surface-muted/50")}>
              <div className="flex items-start justify-between gap-2">
                <span className="flex size-9 items-center justify-center rounded-lg bg-primary/10 text-primary [&_svg]:size-[18px]">{CATEGORY_ICON[c.category] ?? <Plug />}</span>
                <StatusBadge {...CONNECTOR_STATE[c.status]} />
              </div>
              <div>
                <p className="font-semibold text-ink">{c.name}</p>
                <p className="text-xs text-subtle">{c.category}</p>
              </div>
              <p className="text-[13px] text-muted">{c.description}</p>
              <p className="mt-auto text-xs text-muted">{c.detail}</p>
            </Card>
          ))}
        </div>
      )}
      <p className="text-xs text-muted">Only the CampusNexus database is live today. "Available" means the architecture is supported and adapters are built per customer; "Coming next" connectors are planned, not implemented.</p>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------------------------
// Knowledge
// ------------------------------------------------------------------------------------------------------------------

const AGENT_COLUMNS: [string, string][] = [
  ["academic", "Academic"], ["attendance", "Attendance"], ["career", "Career"], ["events", "Events"], ["campus_services", "Campus services"], ["enquiry", "Enquiry"],
];

export function AdminKnowledgePage() {
  const { data, isLoading, isError, error, refetch } = useAdminKnowledge();
  return (
    <div className="space-y-6">
      <PageTitle title="Knowledge Hub" description="Institutional policies that agents cite as evidence. Agents never answer policy questions from model memory." />
      <Card className="flex flex-wrap items-center gap-2 p-4">
        {["Institution documents", "Chunk + embed", "Organization knowledge scope", "Agent retrieval with citations"].map((step, i, all) => (
          <Fragment key={step}>
            <span className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface-muted px-2.5 py-1.5 text-[13px] text-ink">
              {step}
              {step === "Organization knowledge scope" && <Badge tone="caution">Roadmap</Badge>}
            </span>
            {i < all.length - 1 && <ArrowRight aria-hidden className="size-3.5 text-subtle" />}
          </Fragment>
        ))}
      </Card>
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {isLoading && <Loading />}
      {data && (
        <>
          <div className="grid grid-cols-2 gap-3 sm:gap-4 md:grid-cols-3">
            <MetricCard label="Policy documents" value={data.documents.length} icon={<BookOpen />} />
            <MetricCard label="Indexed evidence chunks" value={data.indexed_chunks} icon={<Layers />} />
            <MetricCard label="Knowledge scope" value="Shared" context="Per-organization scope: next step" icon={<Gauge />} />
          </div>
          <Section title="Sources and agent access" description={data.scope_note}>
            <div className="overflow-x-auto">
              <DataTable columns={["Policy", "Version", ...AGENT_COLUMNS.map(([, label]) => label)]} caption="Knowledge sources">
                {data.documents.map((d) => (
                  <tr key={d.document_id}>
                    <td><CellTitle title={d.title} subtitle={d.effective_from ? `Effective ${d.effective_from}` : titleCase(d.document_type)} /></td>
                    <td className="text-muted">{d.version ?? "—"}</td>
                    {AGENT_COLUMNS.map(([key, label]) => (
                      <td key={key} aria-label={`${label}: ${d.agents[key] ? "access" : "no access"}`}>
                        {d.agents[key] ? <Check className="size-4 text-success-strong" /> : <Minus className="size-4 text-subtle" />}
                      </td>
                    ))}
                  </tr>
                ))}
              </DataTable>
            </div>
          </Section>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------------------------
// Control Tower
// ------------------------------------------------------------------------------------------------------------------

function RunPath({ run }: { run: AgentRunRow }) {
  const steps = [
    { label: "Request", detail: `${run.agent_name} · ${run.organization ?? "organization"}` },
    { label: "Routing decision", detail: `${titleCase(run.intelligence)} · ${run.models.join(", ") || "No-AI"}` },
    { label: "Agent", detail: run.agent_name },
    { label: "Tools", detail: "Read-only lookups (DB + policy evidence)" },
    { label: "Verification", detail: run.status === "succeeded" ? "Deterministic verifier passed" : "Failed visibly" },
    { label: "Approval", detail: run.requires_approval ? "Required before any action" : "Not needed (answer only)" },
    { label: "Final action", detail: run.status === "succeeded" ? "Answer delivered + audited" : "No answer fabricated" },
  ];
  return (
    <ol className="flex flex-wrap items-stretch gap-2 px-5 py-4">
      {steps.map((s, i) => (
        <Fragment key={s.label}>
          <li className="min-w-[8.5rem] flex-1 rounded-lg border border-border bg-surface px-3 py-2">
            <p className="text-xs font-semibold text-ink">{s.label}</p>
            <p className="mt-0.5 text-xs text-muted">{s.detail}</p>
          </li>
          {i < steps.length - 1 && <ArrowRight aria-hidden className="size-3.5 self-center text-subtle" />}
        </Fragment>
      ))}
    </ol>
  );
}

export function AdminControlTowerPage() {
  const { data, isLoading, isError, error, refetch } = useAdminAIOperations();
  const [open, setOpen] = useState<string | null>(null);
  const runs = data?.agent_runs ?? [];
  const usage = data?.usage;
  const succeeded = runs.filter((r) => r.status === "succeeded").length;
  const latencies = runs.map((r) => r.latency_ms).filter((v): v is number => v != null);
  return (
    <div className="space-y-6">
      <PageTitle title="Control Tower" description="Every agent run with its routing decision, model, latency, cost and approval requirement."
                 action={<Button variant="outline" size="sm" onClick={() => void refetch()}><RefreshCw /> Refresh</Button>} />
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {isLoading && <MetricSkeletons count={6} />}
      {data && (
        <>
          <div className="grid grid-cols-2 gap-3 sm:gap-4 md:grid-cols-3 2xl:grid-cols-6">
            <MetricCard label="Agent runs" value={runs.length} context="recent deployed-agent runs" icon={<Bot />} />
            <MetricCard label="Success rate" value={runs.length ? `${Math.round((100 * succeeded) / runs.length)}%` : "—"} icon={<BadgeCheck />} />
            <MetricCard label="Average latency" value={latencies.length ? `${Math.round(latencies.reduce((a, b) => a + b, 0) / latencies.length)} ms` : "—"} icon={<Timer />} />
            <MetricCard label="Human approvals" value={data.pending_approvals} context={`${data.stale_approvals} expired`} icon={<ShieldCheck />} />
            <MetricCard label="AI spend" value={usage?.estimated_cost_usd == null ? "$0.00" : `$${usage.estimated_cost_usd.toFixed(4)}`}
                        context={usage?.cost_available_for ? "estimated from tokens" : "no token-reported calls"} icon={<CircleDollarSign />} />
            <MetricCard label="No-AI resolution" value={usage?.no_ai_percent == null ? "—" : `${usage.no_ai_percent}%`} icon={<Zap />} />
          </div>
          <Section title="Agent runs" description="Select a run to see its path.">
            {runs.length === 0 ? (
              <EmptyState compact title="No agent runs yet" description="Ask a deployed agent a question to see its run here." />
            ) : (
              <div className="overflow-x-auto">
                <DataTable columns={["", "Agent", "Trigger / user", "Intelligence", "Model", "Status", "Latency", "Cost", "Approval"]} caption="Agent runs">
                  {runs.map((r) => (
                    <Fragment key={r.run_id}>
                      <tr className="cursor-pointer" onClick={() => setOpen(open === r.run_id ? null : r.run_id)}>
                        <td className="w-8">
                          <button type="button" aria-label={`${open === r.run_id ? "Hide" : "Show"} the path of ${r.run_id}`} aria-expanded={open === r.run_id}
                                  onClick={(e) => { e.stopPropagation(); setOpen(open === r.run_id ? null : r.run_id); }}
                                  className="rounded p-0.5 text-muted hover:text-ink focus-visible:ring-2 focus-visible:ring-primary/40 focus-visible:outline-none">
                            {open === r.run_id ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
                          </button>
                        </td>
                        <td><CellTitle title={r.agent_name} subtitle={formatDateTime(r.created_at)} /></td>
                        <td className="text-muted">{r.organization ?? "—"}</td>
                        <td><LevelBadge level={r.intelligence} /></td>
                        <td className="font-mono text-xs text-muted">{r.models.join(", ") || "No-AI"}</td>
                        <td><StatusBadge label={titleCase(r.status)} tone={r.status === "succeeded" ? "success" : "danger"} /></td>
                        <td className="tabular-nums">{r.latency_ms == null ? "—" : `${r.latency_ms} ms`}</td>
                        <td className="tabular-nums">{r.estimated_cost_usd == null ? "Unavailable" : `$${r.estimated_cost_usd.toFixed(5)}`}</td>
                        <td>{r.requires_approval ? "Required" : "None"}</td>
                      </tr>
                      {open === r.run_id && (
                        <tr>
                          <td colSpan={9} className="bg-surface-muted/60 p-0">
                            <RunPath run={r} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  ))}
                </DataTable>
              </div>
            )}
          </Section>
          <ControlTower rows={data.control_tower} />
        </>
      )}
    </div>
  );
}

export function AdminDeployedAgentsPage() {
  const { data, isLoading, isError, error, refetch } = useAdminDeployments();
  const ops = useAdminAIOperations();
  return (
    <div className="space-y-6">
      <PageTitle title="Deployed agents" description="The AI workforce running for your institution."
                 action={<Link to="/admin/agent-catalog"><Button size="sm"><Boxes /> Open Agent Catalog</Button></Link>} />
      {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
      {isLoading && <Loading />}
      {data && <DeployedAgents rows={data} />}
      {ops.data && <AgentRuns rows={ops.data.agent_runs} />}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------------------------
// Institution value, positioning, business model, tenancy status
// ------------------------------------------------------------------------------------------------------------------

const VALUE = [
  { title: "Reduce administrative load", body: "Self-service answers and automatically routed requests, with humans only where a decision is needed.", icon: <Users /> },
  { title: "Prevent missed risks", body: "Attendance, placement deadlines, approvals and grievance SLAs are monitored proactively.", icon: <ShieldAlert /> },
  { title: "Preserve existing investment", body: "Connector-based integration above your ERP, SIS and LMS — no rip-and-replace.", icon: <Plug /> },
  { title: "Control AI cost", body: "No-AI / 20B / 120B adaptive routing plus per-institution budgets keep spend predictable.", icon: <PiggyBank /> },
];

const COMPARISON: [string, string, string, string][] = [
  ["Institutional records", "Native (system of record)", "Not institution-specific", "Connects to them"],
  ["Natural-language interaction", "Limited / varies", "Native", "Native, institution-aware"],
  ["Cross-system automation", "Integration-dependent", "Not provided", "CampusNexus focus"],
  ["Deterministic eligibility rules", "Existing business logic", "Not provided", "Native, verified"],
  ["Approval workflows", "Often available", "Not provided", "Native, routed with SLA"],
  ["Multi-agent orchestration", "Varies", "Not institution-specific", "Native"],
  ["AI budget control", "Not typical", "Per-user plans", "Per institution + per agent"],
  ["Human approval before actions", "Workflow-dependent", "Not provided", "Enforced (Approval Gate)"],
  ["Full action audit", "System-dependent", "Not provided", "Every action + decision"],
  ["Works above existing ERP", "—", "No institutional integration by default", "Designed for it"],
];

const PLANS = [
  { name: "Pilot", price: "₹2.4L / year", features: ["Up to ~500 users", "Core agents", "Workflow automation", "Basic connectors", "Control Tower"] },
  { name: "Growth", price: "₹3.6L / year", features: ["More users", "Full Agent Catalog", "Advanced workflows", "Higher AI allowance", "Advanced analytics"], highlight: true },
  { name: "Enterprise", price: "₹5L+ / year · custom", features: ["Large universities", "Private deployment", "Custom connectors", "SLA / support", "Hybrid / self-hosted AI options"] },
];

const inr = (n: number) => `₹${Math.round(n).toLocaleString("en-IN")}`;

function UnitEconomics() {
  const [acv, setAcv] = useState(300000);
  const [costs, setCosts] = useState({ ai: 2000, cloud: 4000, messaging: 2000, support: 4000 });
  const monthly = acv / 12;
  const cogs = costs.ai + costs.cloud + costs.messaging + costs.support;
  const contribution = monthly - cogs;
  const margin = monthly > 0 ? Math.round((100 * contribution) / monthly) : 0;
  const field = (key: keyof typeof costs, label: string) => (
    <div className="flex items-center justify-between gap-3">
      <Label htmlFor={`ue-${key}`} className="font-normal text-muted">{label}</Label>
      <Input id={`ue-${key}`} inputMode="numeric" className="h-8 w-28 text-right tabular-nums" value={costs[key]}
             onChange={(e) => setCosts({ ...costs, [key]: Math.max(0, Number(e.target.value) || 0) })} />
    </div>
  );
  return (
    <Section title="Illustrative 500-user unit economics" description="Edit the assumptions; figures recalculate. Illustrative — not measured.">
      <div className="grid grid-cols-1 gap-6 p-5 md:grid-cols-2">
        <div className="space-y-2.5">
          <div className="flex items-center justify-between gap-3">
            <Label htmlFor="ue-acv" className="font-normal text-muted">Annual contract value</Label>
            <Input id="ue-acv" inputMode="numeric" className="h-8 w-28 text-right tabular-nums" value={acv} onChange={(e) => setAcv(Math.max(0, Number(e.target.value) || 0))} />
          </div>
          <p className="flex justify-between text-sm"><span className="text-muted">Monthly revenue</span><span className="font-semibold tabular-nums">{inr(monthly)}</span></p>
          <p className="pt-2 text-xs font-semibold tracking-wide text-muted uppercase">Monthly cost of service</p>
          {field("ai", "AI usage")}
          {field("cloud", "Cloud / database")}
          {field("messaging", "Messaging / monitoring")}
          {field("support", "Support allocation")}
        </div>
        <div className="flex flex-col justify-center gap-3 rounded-lg bg-surface-muted p-5">
          <p className="flex justify-between text-sm"><span className="text-muted">Estimated COGS</span><span className="font-semibold tabular-nums">{inr(cogs)}</span></p>
          <p className="flex justify-between text-sm"><span className="text-muted">Contribution</span><span className="font-semibold tabular-nums">{inr(contribution)} / month</span></p>
          <p className="flex items-baseline justify-between"><span className="text-sm text-muted">Illustrative margin</span><span className={cn("text-3xl font-semibold tabular-nums", margin >= 0 ? "text-ink" : "text-danger-strong")}>~{margin}%</span></p>
          <p className="text-xs text-muted">Adaptive routing keeps deterministic operations at zero LLM cost, so AI usage stays a small share of COGS. Actual production costs require real customer telemetry.</p>
        </div>
      </div>
    </Section>
  );
}

const TENANCY_DONE = ["Organization + membership model", "Organization-aware sign-in tokens", "Tenant-bound database sessions", "Automatic ORM organization filtering", "Isolation tests on critical demo paths"];
const TENANCY_NEXT = ["Tenant propagation on low-priority paths", "Tenant-scoped institutional knowledge (RAG)", "Supabase Phase 22 migration", "PostgreSQL row-level security app role", "Full penetration / isolation suite"];

export function AdminValuePage() {
  return (
    <div className="space-y-6">
      <PageTitle title="Institution value" description="Why CampusNexus, who it serves, and how it is sold." />
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        {VALUE.map((v) => (
          <Card key={v.title} className="flex h-full flex-col gap-2 p-5">
            <span className="flex size-9 items-center justify-center rounded-lg bg-primary/10 text-primary [&_svg]:size-[18px]">{v.icon}</span>
            <p className="font-semibold text-ink">{v.title}</p>
            <p className="text-[13px] text-muted">{v.body}</p>
          </Card>
        ))}
      </div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <Card className="p-5"><p className="text-xs font-semibold tracking-wide text-muted uppercase">Users</p><p className="mt-1 text-sm text-ink">Student · Faculty · HOD · Admin</p></Card>
        <Card className="p-5"><p className="text-xs font-semibold tracking-wide text-muted uppercase">Buyer</p><p className="mt-1 text-sm text-ink">Principal · Director · Registrar · CIO / IT Head · Management</p></Card>
        <Card className="p-5"><p className="text-xs font-semibold tracking-wide text-muted uppercase">Commercial model</p><p className="mt-1 text-sm text-ink">Institutional annual SaaS license</p></Card>
      </div>

      <Section title="Why CampusNexus" description="CampusNexus complements systems of record instead of competing with them head-on.">
        <div className="overflow-x-auto">
          <DataTable columns={["Capability", "Traditional ERP / SIS", "Generic ChatGPT", "CampusNexus"]} caption="Capability comparison">
            {COMPARISON.map(([capability, erp, chat, us]) => (
              <tr key={capability}>
                <td className="font-medium text-ink">{capability}</td>
                <td className="text-muted">{erp}</td>
                <td className="text-muted">{chat}</td>
                <td><span className="inline-flex items-center gap-1.5 font-medium text-ink"><Check className="size-4 text-success-strong" aria-hidden />{us}</span></td>
              </tr>
            ))}
          </DataTable>
        </div>
        <p className="border-t border-border px-5 py-3 text-xs text-muted">Traditional ERP/SIS (e.g. Ellucian) are systems of record and institutional management; generic ChatGPT is general conversational AI. Capabilities vary by product and configuration.</p>
      </Section>

      <Section title="Business model" description="Target institution software budget: ₹2–5 lakh / year." action={<Badge tone="caution">Illustrative — not yet market validated</Badge>}>
        <div className="grid grid-cols-1 gap-4 p-5 md:grid-cols-3">
          {PLANS.map((p) => (
            <div key={p.name} className={cn("flex h-full flex-col rounded-card border p-5", p.highlight ? "border-primary shadow-[var(--shadow-card)]" : "border-border")}>
              <p className="flex items-center justify-between font-semibold text-ink">{p.name}{p.highlight && <Badge tone="primary">Most institutions</Badge>}</p>
              <p className="mt-2 text-2xl font-semibold tracking-tight text-ink">{p.price}</p>
              <ul className="mt-3 space-y-1.5">
                {p.features.map((f) => (
                  <li key={f} className="flex items-center gap-2 text-[13px] text-muted"><Check className="size-3.5 text-primary" aria-hidden />{f}</li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </Section>
      <UnitEconomics />

      <Section title="Multi-tenancy status" description="The application layer is organization-aware and isolated on critical paths; production database-level RLS is the next deployment hardening step.">
        <div className="grid grid-cols-1 gap-6 p-5 md:grid-cols-2">
          <div>
            <p className="mb-2 flex items-center gap-2 text-sm font-semibold text-ink"><BadgeCheck className="size-4 text-success-strong" /> Tenant-aware architecture implemented</p>
            <ul className="space-y-1.5">{TENANCY_DONE.map((t) => <li key={t} className="flex items-center gap-2 text-[13px] text-muted"><Check className="size-3.5 text-success-strong" />{t}</li>)}</ul>
          </div>
          <div>
            <p className="mb-2 flex items-center gap-2 text-sm font-semibold text-ink"><Hourglass className="size-4 text-caution-strong" /> Production hardening remaining</p>
            <ul className="space-y-1.5">{TENANCY_NEXT.map((t) => <li key={t} className="flex items-center gap-2 text-[13px] text-muted"><Minus className="size-3.5 text-subtle" />{t}</li>)}</ul>
          </div>
        </div>
      </Section>
    </div>
  );
}
