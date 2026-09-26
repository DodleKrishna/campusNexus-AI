import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Boxes, ShieldCheck } from "lucide-react";
import { useState, type ReactNode } from "react";
import { ApiError } from "@/api/client";
import { api, queryKeys } from "@/api/endpoints";
import { Badge, StatusBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { CellTitle, DataTable } from "@/components/ui/data-table";
import { Dialog } from "@/components/ui/dialog";
import { Input, Label, Select } from "@/components/ui/input";
import { EmptyState, Notice } from "@/components/ui/states";
import { LevelBadge } from "@/features/admin/IntelligenceOps";
import type { AgentRunRow, CatalogEntry, DeploymentConfig, DeploymentView, IntelligenceLevel } from "@/types/api";
import { formatDateTime, titleCase } from "@/utils/format";

const money = (v: number | null | undefined) =>
  v === null || v === undefined ? "Unavailable" : `$${v.toFixed(v > 0 && v < 0.01 ? 5 : 2)}`;
const budgetLabel = (v: number | null | undefined) => (v === null || v === undefined ? "No limit" : money(v));
const orDash = (v: number | null | undefined, suffix = "") => (v === null || v === undefined ? "—" : `${v}${suffix}`);

const LEVEL_OPTIONS: { value: IntelligenceLevel; label: string }[] = [
  { value: "no_ai", label: "No AI (deterministic)" },
  { value: "light", label: "Light · openai/gpt-oss-20b" },
  { value: "advanced", label: "Advanced · openai/gpt-oss-120b" },
];

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card className="overflow-hidden">
      <CardHeader title={title} />
      <div className="border-t border-border">{children}</div>
    </Card>
  );
}

function StatusPill({ status }: { status: string }) {
  return <StatusBadge label={status === "active" ? "Active" : "Paused"} tone={status === "active" ? "success" : "warning"} />;
}

function ConfigureDialog({ entry, onClose }: { entry: CatalogEntry; onClose: () => void }) {
  const current = entry.deployment;
  const client = useQueryClient();
  const [status, setStatus] = useState<"active" | "paused">(current?.status ?? "active");
  const [level, setLevel] = useState<IntelligenceLevel>(current?.intelligence_level ?? entry.intelligence_level);
  const [budget, setBudget] = useState(current?.monthly_budget_usd == null ? "" : String(current.monthly_budget_usd));
  const [approval, setApproval] = useState(current?.requires_approval ?? entry.requires_approval);
  const save = useMutation({
    mutationFn: () => {
      const config: DeploymentConfig = {
        status,
        intelligence_level: level,
        requires_approval: approval,
        monthly_budget_usd: budget.trim() === "" ? null : Math.max(0, Number(budget)),
      };
      return current ? api.adminConfigureAgent(current.id, config) : api.adminDeployAgent(entry.key, config);
    },
    onSuccess: () => {
      for (const key of ["agents-catalog", "deployments", "ai"]) void client.invalidateQueries({ queryKey: queryKeys.admin(key) });
      onClose();
    },
  });
  return (
    <Dialog open onClose={onClose} title={`${current ? "Configure" : "Deploy"} ${entry.name}`}>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <p className="text-[13px] text-muted">{entry.purpose}</p>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="deploy-status">Status</Label>
            <Select id="deploy-status" className="w-full" value={status} onChange={(e) => setStatus(e.target.value as "active" | "paused")}>
              <option value="active">Active</option>
              <option value="paused">Paused</option>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="deploy-level">Intelligence level</Label>
            <Select id="deploy-level" className="w-full" value={level} onChange={(e) => setLevel(e.target.value as IntelligenceLevel)}>
              {LEVEL_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="deploy-budget">Monthly budget (USD)</Label>
            <Input id="deploy-budget" inputMode="decimal" placeholder="No limit" value={budget} onChange={(e) => setBudget(e.target.value)} />
          </div>
          <label className="flex items-center gap-2 self-end pb-2 text-sm text-ink">
            <input type="checkbox" className="size-4 accent-primary" checked={approval} onChange={(e) => setApproval(e.target.checked)} />
            Require human approval for actions
          </label>
        </div>
        <p className="text-xs text-muted">The institution AI budget is enforced; the agent budget is recorded for planning.</p>
        {save.isError && <Notice tone="warning">{(save.error as ApiError).message}</Notice>}
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={save.isPending}>
            {current ? "Save configuration" : "Deploy agent"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function ProductCard({ entry, onConfigure }: { entry: CatalogEntry; onConfigure: () => void }) {
  const d = entry.deployment;
  return (
    <Card className="flex h-full flex-col gap-3 p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-semibold text-ink">{entry.name}</p>
          <p className="mt-1 text-[13px] leading-5 text-muted">{entry.purpose}</p>
        </div>
        {d ? <StatusPill status={d.status} /> : <StatusBadge label="Not deployed" tone="neutral" />}
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        <LevelBadge level={d?.intelligence_level ?? entry.intelligence_level} />
        {(d?.requires_approval ?? entry.requires_approval) && (
          <Badge tone="warning">
            <ShieldCheck className="size-3" aria-hidden /> Human approval
          </Badge>
        )}
        <Badge>Budget {budgetLabel(d?.monthly_budget_usd)}</Badge>
      </div>
      <div className="mt-auto flex items-center justify-between gap-2 pt-1">
        <span className="text-xs text-muted">{d ? `Deployed · ${d.runs} runs` : "Available to deploy"}</span>
        <Button size="sm" variant={d ? "outline" : "primary"} onClick={onConfigure} aria-label={`${d ? "Configure" : "Deploy"} ${entry.name}`}>
          {d ? "Configure" : "Deploy"}
        </Button>
      </div>
    </Card>
  );
}

export function DeployableCatalog({ entries }: { entries: CatalogEntry[] }) {
  const [editing, setEditing] = useState<CatalogEntry | null>(null);
  return (
    <div>
      <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-ink">
        <Boxes className="size-4 text-primary" aria-hidden /> Product agents ({entries.length})
      </h2>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {entries.map((e) => (
          <ProductCard key={e.key} entry={e} onConfigure={() => setEditing(e)} />
        ))}
      </div>
      {editing && <ConfigureDialog entry={editing} onClose={() => setEditing(null)} />}
    </div>
  );
}

export function DeployedAgents({ rows }: { rows: DeploymentView[] }) {
  return (
    <Section title="Deployed Agents">
      {rows.length === 0 ? (
        <EmptyState compact title="No agents deployed" description="Deploy an agent from the catalog below." />
      ) : (
        <div className="overflow-x-auto">
          <DataTable columns={["Agent", "Status", "Intelligence", "Budget", "Runs", "Success", "Est. cost"]} caption="Deployed agents">
            {rows.map((d) => (
              <tr key={d.id}>
                <td>
                  <CellTitle title={d.display_name} subtitle={d.allowed_roles.map(titleCase).join(", ")} />
                </td>
                <td>
                  <StatusPill status={d.status} />
                </td>
                <td>
                  <LevelBadge level={d.intelligence_level} />
                </td>
                <td className="tabular-nums">{budgetLabel(d.monthly_budget_usd)}</td>
                <td className="tabular-nums">{d.runs}</td>
                <td className="tabular-nums">{orDash(d.success_rate_percent, "%")}</td>
                <td className="tabular-nums">{d.runs ? money(d.estimated_cost_usd) : "—"}</td>
              </tr>
            ))}
          </DataTable>
        </div>
      )}
    </Section>
  );
}

export function AgentRuns({ rows }: { rows?: AgentRunRow[] }) {
  if (!rows) return null;
  return (
    <Section title="Control Tower · deployed agent runs">
      {rows.length === 0 ? (
        <EmptyState compact title="No agent runs yet" description="Runs of deployed agents appear here with the model they used." />
      ) : (
        <div className="overflow-x-auto">
          <DataTable columns={["Agent", "Organization", "Intelligence", "Status", "Latency", "Est. cost", "Approval"]} caption="Deployed agent runs">
            {rows.map((r) => (
              <tr key={r.run_id}>
                <td>
                  <CellTitle title={r.agent_name} subtitle={`${r.run_id} · ${formatDateTime(r.created_at)}`} />
                </td>
                <td className="text-muted">{r.organization ?? "—"}</td>
                <td>
                  <LevelBadge level={r.intelligence} />
                  <p className="mt-1 truncate text-xs text-muted">{r.models.join(", ") || "No-AI"}</p>
                </td>
                <td>
                  <StatusBadge label={titleCase(r.status)} tone={r.status === "succeeded" ? "success" : "danger"} />
                </td>
                <td className="tabular-nums">{orDash(r.latency_ms, " ms")}</td>
                <td className="tabular-nums">{money(r.estimated_cost_usd)}</td>
                <td>{r.requires_approval ? "Required" : "None"}</td>
              </tr>
            ))}
          </DataTable>
        </div>
      )}
    </Section>
  );
}
