import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Boxes, Cpu, ShieldCheck, Sparkles, Zap } from "lucide-react";
import { useState } from "react";
import { ApiError } from "@/api/client";
import { api, queryKeys } from "@/api/endpoints";
import { MISSION_STATUS } from "@/components/dashboard/status";
import { Badge, StatusBadge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { CellTitle, DataTable } from "@/components/ui/data-table";
import { Input } from "@/components/ui/input";
import { EmptyState, Notice } from "@/components/ui/states";
import type { AIUsageSummary, AgentCatalogView, CatalogAgent, ControlTowerMission } from "@/types/api";
import { cn } from "@/utils/cn";
import { formatDateTime, titleCase } from "@/utils/format";

const LEVEL: Record<string, { label: string; tone: BadgeTone }> = {
  no_ai: { label: "No AI", tone: "success" },
  light: { label: "20B · Light", tone: "info" },
  advanced: { label: "120B · Advanced", tone: "accent" },
};

export function LevelBadge({ level }: { level: string }) {
  const meta = LEVEL[level] ?? { label: titleCase(level), tone: "neutral" as BadgeTone };
  return <Badge tone={meta.tone}>{meta.label}</Badge>;
}

const money = (v: number | null | undefined) =>
  v === null || v === undefined ? "Unavailable" : `$${v.toFixed(v > 0 && v < 0.01 ? 5 : 2)}`;
const orDash = (v: number | null | undefined, suffix = "") => (v === null || v === undefined ? "—" : `${v}${suffix}`);

function Section({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <Card className="overflow-hidden">
      <CardHeader title={title} action={action} />
      <div className="border-t border-border">{children}</div>
    </Card>
  );
}

export function IntelligencePanel({ usage, routing }: { usage?: AIUsageSummary; routing?: Record<string, string> }) {
  if (!usage) return null;
  const metrics = [
    { label: "Total requests", value: usage.total_requests, note: "AI calls + No-AI resolutions" },
    { label: "Resolved without AI", value: orDash(usage.no_ai_percent, "%"), note: `${usage.no_ai_count} requests` },
    { label: "20B calls", value: usage.light_calls, note: routing?.light ?? "light model" },
    { label: "120B calls", value: usage.advanced_calls, note: routing?.advanced ?? "advanced model" },
    { label: "Estimated AI cost", value: money(usage.estimated_cost_usd), note: usage.cost_available_for ? `${usage.cost_available_for} calls with token counts` : "provider reported no tokens" },
    { label: "Average latency", value: orDash(usage.average_latency_ms, " ms"), note: "per AI call" },
    { label: "Success rate", value: orDash(usage.success_rate_percent, "%"), note: "of AI calls" },
  ];
  return (
    <Section title="Adaptive intelligence routing" action={<BudgetControl usage={usage} />}>
      <div className="flex flex-wrap items-center gap-2 px-5 pt-4 text-xs text-muted">
        <Zap className="size-3.5 text-success-strong" aria-hidden /> Deterministic rules first
        <ArrowRight className="size-3" aria-hidden /> <Cpu className="size-3.5 text-primary" aria-hidden /> {routing?.light ?? "gpt-oss-20b"}
        <ArrowRight className="size-3" aria-hidden /> <Sparkles className="size-3.5 text-accent-hover" aria-hidden /> {routing?.advanced ?? "gpt-oss-120b"}
        <span className="ml-1">· chosen before each call, never silently downgraded</span>
      </div>
      <dl className="grid grid-cols-2 gap-px sm:grid-cols-4 lg:grid-cols-7">
        {metrics.map((m) => (
          <div key={m.label} className="min-w-0 px-5 py-4">
            <dt className="text-xs font-medium text-muted">{m.label}</dt>
            <dd className="mt-1 truncate text-xl font-semibold tabular-nums text-ink">{m.value}</dd>
            <dd className="truncate text-xs text-muted" title={m.note}>{m.note}</dd>
          </div>
        ))}
      </dl>
    </Section>
  );
}

function BudgetControl({ usage }: { usage: AIUsageSummary }) {
  const client = useQueryClient();
  const [value, setValue] = useState<string>(usage.monthly_budget_usd === null ? "" : String(usage.monthly_budget_usd));
  const save = useMutation({
    mutationFn: (budget: number | null) => api.adminSetAIBudget(budget),
    onSuccess: () => void client.invalidateQueries({ queryKey: queryKeys.admin("ai") }),
  });
  const exhausted = usage.monthly_budget_usd !== null && usage.month_spend_usd >= usage.monthly_budget_usd;
  return (
    <div className="flex flex-wrap items-center justify-end gap-2">
      <span className="text-xs text-muted">
        Month spend {money(usage.month_spend_usd)} / {usage.monthly_budget_usd === null ? "no limit" : money(usage.monthly_budget_usd)}
      </span>
      {exhausted && <StatusBadge label="AI_BUDGET_EXCEEDED" tone="danger" />}
      <Input
        aria-label="Monthly AI budget in USD"
        className="h-8 w-24"
        inputMode="decimal"
        placeholder="No limit"
        value={value}
        onChange={(e) => setValue(e.target.value)}
      />
      <Button size="sm" variant="outline" disabled={save.isPending} onClick={() => save.mutate(value.trim() === "" ? null : Math.max(0, Number(value)))}>
        Set budget
      </Button>
      {save.isError && <span className="text-xs text-danger-strong">{(save.error as ApiError).message}</span>}
    </div>
  );
}

export function ControlTower({ rows }: { rows?: ControlTowerMission[] }) {
  if (!rows) return null;
  return (
    <Section title="Control Tower · recent agent missions">
      {rows.length === 0 ? (
        <EmptyState compact title="No missions yet" description="Missions started by students appear here with the AI they used." />
      ) : (
        <div className="overflow-x-auto">
          <DataTable columns={["Mission", "Status", "Role", "Organization", "Intelligence", "Est. cost", "Latency", "Approvals"]} caption="Recent agent missions">
            {rows.map((m) => (
              <tr key={m.mission_id}>
                <td className="max-w-[22rem]">
                  <CellTitle title={m.goal} subtitle={`${m.mission_id} · ${formatDateTime(m.created_at)}`} />
                </td>
                <td><StatusBadge {...MISSION_STATUS(m.status)} /></td>
                <td>{titleCase(m.role)}</td>
                <td className="text-muted">{m.organization ?? "—"}</td>
                <td>
                  <LevelBadge level={m.intelligence} />
                  <p className="mt-1 truncate text-xs text-muted" title={m.models.join(", ")}>{m.models.join(", ")}</p>
                </td>
                <td className="tabular-nums">{m.ai_calls ? money(m.estimated_cost_usd) : "—"}</td>
                <td className="tabular-nums">{orDash(m.latency_ms, " ms")}</td>
                <td className={cn("tabular-nums", m.approvals_required ? "font-medium text-ink" : "text-muted")}>
                  {m.approvals_required ? `${m.approvals_required} required` : "None"}
                </td>
              </tr>
            ))}
          </DataTable>
        </div>
      )}
    </Section>
  );
}

function AgentCard({ agent }: { agent: CatalogAgent }) {
  return (
    <Card className={cn("flex h-full flex-col gap-3 p-5", !agent.deployable && "bg-surface-muted/60")}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-semibold text-ink">{agent.name}</p>
          <p className="mt-1 text-[13px] leading-5 text-muted">{agent.purpose}</p>
        </div>
        <StatusBadge label={agent.deployable ? "Deployed" : "Internal"} tone={agent.deployable ? "success" : "neutral"} />
      </div>
      <div className="mt-auto flex flex-wrap items-center gap-1.5">
        <LevelBadge level={agent.intelligence_level} />
        {agent.requires_approval && (
          <Badge tone="warning">
            <ShieldCheck className="size-3" aria-hidden /> Human approval
          </Badge>
        )}
        {agent.allowed_roles.map((r) => (
          <Badge key={r}>{titleCase(r)}</Badge>
        ))}
      </div>
    </Card>
  );
}

export function AgentCatalog({ data }: { data: AgentCatalogView }) {
  return (
    <div className="space-y-6">
      <Section title="How a request flows">
        <ol className="flex flex-wrap items-center gap-2 px-5 py-4 text-[13px] text-ink">
          {data.flow.map((step, i) => (
            <li key={step} className="flex items-center gap-2">
              <span className="rounded-md border border-border bg-surface px-2 py-1">{step}</span>
              {i < data.flow.length - 1 && <ArrowRight className="size-3.5 text-subtle" aria-hidden />}
            </li>
          ))}
        </ol>
      </Section>
      <div>
        <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-ink">
          <Boxes className="size-4 text-primary" aria-hidden /> Product agents ({data.agents.length})
        </h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {data.agents.map((a) => (
            <AgentCard key={a.key} agent={a} />
          ))}
        </div>
      </div>
      <div>
        <h2 className="mb-1 text-sm font-semibold text-ink">Internal infrastructure</h2>
        <Notice tone="info" className="mb-3">
          These components are never deployable or directly reachable: agents only reach them through the orchestrator.
        </Notice>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          {data.internal_components.map((a) => (
            <AgentCard key={a.key} agent={a} />
          ))}
        </div>
      </div>
    </div>
  );
}
