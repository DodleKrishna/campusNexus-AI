import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { STUDENT, mockApi, renderApp, signIn } from "@/test/utils";
import type { AIOperations, AgentCatalogView, AuthUser, CatalogEntry, DeploymentView } from "@/types/api";

afterEach(() => vi.unstubAllGlobals());

const HEALTH = { ready: true, llm: { provider: "mock", live: false, model: null } };
const ORG = { id: 1, slug: "campusnexus-demo", name: "CampusNexus Demo Institution" };
const ADMIN: AuthUser = {
  ...STUDENT, id: 4, email: "admin@campusnexus.local", role: "admin", display_name: "Priya Desai", student_id: null,
  department_code: null, department_name: null, home_route: "/admin", organization: ORG,
};

const template = (key: string, name: string, level: "no_ai" | "light" | "advanced", deployable = true) => ({
  key, name, purpose: `${name} purpose`, status: deployable ? "active" : "internal", intelligence_level: level,
  allowed_roles: deployable ? ["student"] : [], requires_approval: false, deployable,
});
const ACADEMIC_DEPLOYMENT: DeploymentView = {
  id: 7, agent_key: "academic", display_name: "Academic Agent", status: "active", intelligence_level: "advanced",
  monthly_budget_usd: 25, requires_approval: false, allowed_roles: ["student"], runs: 3, success_rate_percent: 100,
  estimated_cost_usd: null, updated_at: "2026-09-26T04:00:00Z",
};
const ENTRIES: CatalogEntry[] = [
  { ...template("academic", "Academic Agent", "advanced"), deployment: ACADEMIC_DEPLOYMENT },
  { ...template("career", "Career / Placement Agent", "advanced"), deployment: null },
];
const STATIC: AgentCatalogView = {
  agents: [], internal_components: [template("action", "Action Agent", "no_ai", false), template("approval_gate", "Approval Gate", "no_ai", false)],
  flow: ["User", "Mission Orchestrator", "Approval Gate (when sensitive)"],
};
const OPS: AIOperations = {
  provider: "mock", model: null, live: false, live_ai_configured: { groq: false, anthropic: false }, missions_by_status: {},
  recent_missions: [], agent_runs_total: 0, agent_runs_failed: 0, failed_missions: 0, provider_errors: 0, rate_limit_incidents: 0,
  recent_provider_errors: [], pending_approvals: 0, stale_approvals: 0, workflow_failures: 0, recent_workflow_failures: [],
  requests_needing_review: 0, rag_chunks: 47, rag_ready: true, database_ready: true, token_usage: "",
  routing: { no_ai: "deterministic rules / workflow", light: "openai/gpt-oss-20b", advanced: "openai/gpt-oss-120b" },
  usage: {
    total_requests: 10, no_ai_count: 4, no_ai_percent: 40, light_calls: 5, advanced_calls: 1, estimated_cost_usd: null, cost_available_for: 0,
    average_latency_ms: 120, success_rate_percent: 100, month_spend_usd: 0, monthly_budget_usd: 0,
  },
  control_tower: [],
  agent_runs: [{
    run_id: "run-abc", agent_key: "academic", agent_name: "Academic Agent", organization: ORG.name, intelligence: "advanced",
    models: ["openai/gpt-oss-120b"], status: "succeeded", latency_ms: 90, estimated_cost_usd: null, requires_approval: false,
    created_at: "2026-09-26T04:00:00Z",
  }],
};

function admin() {
  signIn(ADMIN);
  const calls: { url: string; method: string; body: unknown }[] = [];
  mockApi((url, init) => {
    const method = init?.method ?? "GET";
    if (method !== "GET") calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : null });
    if (url.endsWith("/auth/me")) return { body: ADMIN };
    if (url.endsWith("/health")) return { body: HEALTH };
    if (url.endsWith("/admin/agents/catalog")) return { body: ENTRIES };
    if (url.endsWith("/admin/agents/deployments")) return { body: [ACADEMIC_DEPLOYMENT] };
    if (url.endsWith("/admin/agent-catalog")) return { body: STATIC };
    if (url.endsWith("/admin/ai-operations")) return { body: OPS };
    if (method !== "GET") return { body: ACADEMIC_DEPLOYMENT };
    return { body: [] };
  });
  return calls;
}

describe("Agent-as-a-Product", () => {
  it("shows deployed and deployable agents, and keeps internal components non-deployable", async () => {
    admin();
    renderApp("/admin/agent-catalog");
    expect(await screen.findByText("Deployed Agents")).toBeInTheDocument();
    expect(await screen.findByText("Product agents (2)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Configure Academic Agent" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deploy Career / Placement Agent" })).toBeInTheDocument();
    expect(screen.getAllByText("Internal")).toHaveLength(2);
    expect(screen.getByRole("link", { name: "Agent Catalog" })).toBeInTheDocument();
  });

  it("configures a deployment and deploys a new agent without ever sending an organization", async () => {
    const calls = admin();
    const user = userEvent.setup();
    renderApp("/admin/agent-catalog");
    await user.click(await screen.findByRole("button", { name: "Configure Academic Agent" }));
    const dialog = await screen.findByRole("dialog");
    await user.selectOptions(within(dialog).getByLabelText("Status"), "paused");
    await user.click(within(dialog).getByRole("button", { name: "Save configuration" }));
    await vi.waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect(patch.url).toMatch(/\/admin\/agents\/deployments\/7$/);
    expect(patch.body).toMatchObject({ status: "paused", intelligence_level: "advanced", monthly_budget_usd: 25 });
    expect(JSON.stringify(patch.body)).not.toMatch(/organization/);

    await user.click(await screen.findByRole("button", { name: "Deploy Career / Placement Agent" }));
    const deploy = await screen.findByRole("dialog");
    await user.click(within(deploy).getByRole("button", { name: "Deploy agent" }));
    await vi.waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/admin/agents/career/deploy"))).toBe(true));
  });

  it("shows deployed agent runs in the Control Tower", async () => {
    admin();
    renderApp("/admin/ai-operations");
    expect(await screen.findByText("Control Tower · deployed agent runs")).toBeInTheDocument();
    expect(screen.getByText("Adaptive intelligence routing")).toBeInTheDocument();
    expect(screen.getByText("AI_BUDGET_EXCEEDED")).toBeInTheDocument();
    expect(screen.getAllByText("Academic Agent").length).toBeGreaterThan(0);
  });
});
