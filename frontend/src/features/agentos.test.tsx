import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { STUDENT, mockApi, renderApp, signIn } from "@/test/utils";
import type { AIOperations, AgentCatalogView, AuthUser } from "@/types/api";

afterEach(() => vi.unstubAllGlobals());

const HEALTH = { ready: true, llm: { provider: "mock", live: false, model: null } };
const ORG = { id: 1, slug: "campusnexus-demo", name: "CampusNexus Demo Institution" };
const ADMIN: AuthUser = {
  ...STUDENT, id: 4, email: "admin@campusnexus.local", role: "admin", display_name: "Priya Desai", student_id: null,
  department_code: null, department_name: null, home_route: "/admin", organization: ORG,
};

const agent = (key: string, name: string, level: "no_ai" | "light" | "advanced", deployable = true) => ({
  key, name, purpose: `${name} purpose`, status: deployable ? "active" : "internal", intelligence_level: level,
  allowed_roles: deployable ? ["student"] : [], requires_approval: key === "events", deployable,
});
const CATALOG: AgentCatalogView = {
  agents: [agent("academic", "Academic Agent", "light"), agent("attendance", "Attendance Agent", "no_ai"), agent("events", "Events Agent", "light"),
    agent("enquiry", "Enquiry Agent", "advanced")],
  internal_components: [agent("action", "Action Agent", "no_ai", false), agent("approval_gate", "Approval Gate", "no_ai", false)],
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
  control_tower: [{
    mission_id: "mission-abc", status: "awaiting_approval", role: "student", organization: ORG.name, goal: "Register me for the coding contest",
    created_at: "2026-09-26T04:00:00Z", intelligence: "advanced", models: ["openai/gpt-oss-120b"], ai_calls: 4, estimated_cost_usd: null,
    latency_ms: 900, approvals_required: 1,
  }],
};

function admin() {
  signIn(ADMIN);
  mockApi((url) => {
    if (url.endsWith("/auth/me")) return { body: ADMIN };
    if (url.endsWith("/health")) return { body: HEALTH };
    if (url.endsWith("/admin/agent-catalog")) return { body: CATALOG };
    if (url.endsWith("/admin/ai-operations")) return { body: OPS };
    return { body: [] };
  });
}

describe("AgentOS surfaces", () => {
  it("lists deployable product agents and keeps internal components non-deployable", async () => {
    admin();
    renderApp("/admin/agent-catalog");
    expect(await screen.findByText("Academic Agent")).toBeInTheDocument();
    expect(screen.getByText("Product agents (4)")).toBeInTheDocument();
    expect(screen.getAllByText("Internal")).toHaveLength(2);
    expect(screen.getByText("120B · Advanced")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Agent Catalog" })).toBeInTheDocument();
  });

  it("shows routing usage, the budget state and the control tower", async () => {
    admin();
    renderApp("/admin/ai-operations");
    expect(await screen.findByText("Adaptive intelligence routing")).toBeInTheDocument();
    expect(screen.getByText("40%")).toBeInTheDocument();
    expect(screen.getByText("AI_BUDGET_EXCEEDED")).toBeInTheDocument(); // budget 0, spend 0: exhausted
    expect(screen.getByText("Register me for the coding contest")).toBeInTheDocument();
    expect(screen.getByText("1 required")).toBeInTheDocument();
    expect(screen.getAllByText("Unavailable").length).toBeGreaterThan(0); // no token counts -> never invented
  });
});
