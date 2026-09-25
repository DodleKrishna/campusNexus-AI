import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PermissionAgentPanel } from "@/features/requests/PermissionAgentPanel";
import { STUDENT, mockApi, renderApp, signIn, withProviders } from "@/test/utils";
import type { AdminDashboard, AIOperations, AuditEntry, AuthUser, DepartmentRow, SystemStatus, WorkflowRequest } from "@/types/api";

afterEach(() => vi.unstubAllGlobals());

const HEALTH = { ready: true, llm: { provider: "mock", live: false, model: null } };
const ADMIN_USER: AuthUser = { ...STUDENT, id: 4, email: "admin@campusnexus.local", role: "admin", display_name: "Priya Desai", student_id: null, department_code: null, department_name: null, home_route: "/admin" };
const HOD_USER: AuthUser = { ...STUDENT, id: 3, email: "hod@campusnexus.local", role: "hod", display_name: "Dr. Kavita Iyer", student_id: null, home_route: "/hod" };

const DASHBOARD: AdminDashboard = {
  date: "2026-09-30", now_local: "10:20", start_grace_minutes: 10, total_students: 28, total_faculty: 13, departments: 4,
  classes_today: 2, active_classes: 0, not_started_classes: 2, completed_classes: 0, cancelled_classes: 0, pending_requests: 1,
  escalations: 0, sla_breaches: 3, system_errors_24h: 1, activity: [], department_filter: null,
};
const CSE: DepartmentRow = {
  department_id: 1, code: "CSE", name: "Computer Science & Engineering", hod_name: "Dr. Kavita Iyer", faculty_count: 6, student_count: 16,
  classes_today: 1, active_classes: 0, not_started_classes: 1, attendance_risk_students: 5, pending_requests: 1, open_complaints: 4, sla_breaches: 2,
};
const HOD_LEAVE: WorkflowRequest = {
  request_id: "REQ-HOD00001", request_type: "hod_leave", type_label: "HOD Leave", status: "pending", title: "HOD Leave for Thu 01 Oct 2026",
  reason: "Personal work", student_id: null, student_name: null, requester_kind: "faculty", requester_name: "Dr. Kavita Iyer", requester_role: "hod",
  department_code: "CSE", reviewer_role: "admin", reviewer_name: "the Administration", routing_basis: "administration",
  routing_note: "Sent to the Administration.", routing_history: [{ at: "2026-09-30T04:40:00Z", basis: "administration", reviewer: "the Administration", note: "Sent to the Administration." }],
  context: {
    event: null, request_date: "2026-10-01", day_part: "full_day", window_start: null, window_end: null, affected_classes: [], timetable_conflict: false,
    student_name: null, student_year: null, student_section: null, department_code: "CSE", requester_kind: "faculty", faculty_name: "Dr. Kavita Iyer",
    faculty_designation: "Professor & Head of Department", faculty_employee_code: "EMP-CSE-001", notes: [],
  },
  created_at: "2026-09-30T04:40:00Z", submitted_at: "2026-09-30T04:41:00Z", decided_at: null, decided_by: null, decision_reason: null,
};

function adminApi(extra: (url: string, init?: RequestInit) => { status?: number; body?: unknown } | undefined = () => undefined) {
  signIn(ADMIN_USER);
  return mockApi((url, init) => {
    const hit = extra(url, init);
    if (hit) return hit;
    if (url.endsWith("/auth/me")) return { body: ADMIN_USER };
    if (url.endsWith("/health")) return { body: HEALTH };
    if (url.endsWith("/admin/notifications")) return { body: [] };
    if (url.endsWith("/admin/departments")) return { body: [CSE] };
    return undefined;
  });
}

describe("Admin console", () => {
  it("keeps non-admins out of /admin", async () => {
    signIn(HOD_USER);
    mockApi((url) => (url.endsWith("/auth/me") ? { body: HOD_USER } : url.endsWith("/health") ? { body: HEALTH } : { body: [] }));
    renderApp("/admin");
    expect(await screen.findByRole("link", { name: "My Requests" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "AI Operations" })).not.toBeInTheDocument();
  });

  it("shows institution metrics from the API", async () => {
    adminApi((url) => (url.includes("/admin/dashboard") ? { body: DASHBOARD } : undefined));
    renderApp("/admin");
    expect(await screen.findByText("Campus overview")).toBeInTheDocument();
    expect((await screen.findByText("Students")).parentElement?.textContent).toContain("28");
    expect(screen.getByText("SLA breaches").parentElement?.textContent).toContain("3");
    for (const label of ["Departments", "Users", "Attendance", "Requests", "Complaints", "AI Operations", "Audit Log", "Agents"]) {
      expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
    }
  });

  it("lists departments with their HOD and risk", async () => {
    adminApi();
    renderApp("/admin/departments");
    const row = (await screen.findByText("Dr. Kavita Iyer")).closest("tr")!;
    expect(within(row).getByText("CSE")).toBeInTheDocument();
    expect(within(row).getByText("2 past SLA")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: /Open/ })).toHaveAttribute("href", "/admin/departments/CSE");
  });

  it("lets the administration approve an HOD request with its routing history", async () => {
    const calls = adminApi((url) => {
      if (url.includes("/requests?box=inbox")) return { body: [HOD_LEAVE] };
      if (url.endsWith("/requests/REQ-HOD00001/approve")) return { body: { ...HOD_LEAVE, status: "approved" } };
      return undefined;
    });
    renderApp("/admin/requests");
    // Phase 20: the inbox is a table; a request opens in a drawer with the decision at its foot.
    await userEvent.click(await screen.findByRole("button", { name: "Review Dr. Kavita Iyer — HOD Leave for Thu 01 Oct 2026" }));
    expect(await screen.findByText("Dr. Kavita Iyer — HOD Leave for Thu 01 Oct 2026")).toBeInTheDocument();
    expect(screen.getByText(/Routing history \(1\)/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(calls.some((c) => c.url.endsWith("/requests/REQ-HOD00001/approve") && c.init?.method === "POST")).toBe(true);
  });

  it("renders the audit log and AI operations from the API", async () => {
    const audit: AuditEntry[] = [
      { timestamp: "2026-09-30T04:34:00Z", source: "operations", actor: "Dr. Ashok Verma", role: "faculty", action: "class_started", target: "attendance_session", reference: "7", outcome: "Dr. Ashok Verma started Computer Networks." },
    ];
    const ai: AIOperations = {
      provider: "mock", model: null, live: false, live_ai_configured: { groq: false, anthropic: false }, missions_by_status: { failed: 1 },
      recent_missions: [], agent_runs_total: 3, agent_runs_failed: 0, failed_missions: 1, provider_errors: 1, rate_limit_incidents: 1,
      recent_provider_errors: [], pending_approvals: 0, stale_approvals: 0, workflow_failures: 0, recent_workflow_failures: [],
      requests_needing_review: 0, rag_chunks: 47, rag_ready: true, database_ready: true, token_usage: "Not recorded by CampusNexus.",
    };
    adminApi((url) => (url.includes("/admin/audit") ? { body: audit } : url.endsWith("/admin/ai-operations") ? { body: ai } : undefined));
    const view = renderApp("/admin/audit");
    expect(await screen.findByText("Dr. Ashok Verma started Computer Networks.")).toBeInTheDocument();
    expect(screen.getByText("class started")).toBeInTheDocument();
    view.unmount();
    renderApp("/admin/ai-operations");
    expect(await screen.findByText(/LLM provider is the offline mock/)).toBeInTheDocument();
    expect(screen.getByText("1 rate-limit incidents")).toBeInTheDocument();
  });

  it("shows whether secrets are configured, never their values", async () => {
    const system: SystemStatus = {
      overall: "degraded",
      components: [{ name: "LLM provider", status: "degraded", detail: "Mock provider (deterministic, not live AI)" }],
      settings: { llm_provider: "mock", live_ai_key_configured: { groq: true, anthropic: false }, jwt_secret_configured: true },
    };
    adminApi((url) => (url.endsWith("/admin/system") ? { body: system } : undefined));
    renderApp("/admin/settings");
    expect(await screen.findByText("groq: yes, anthropic: no")).toBeInTheDocument();
    expect(screen.getByText("JWT secret configured").nextElementSibling?.textContent).toBe("Yes");
    expect(document.body.textContent).not.toMatch(/gsk_|secret_value/);
  });
});

describe("HOD -> Administration request", () => {
  it("previews the HOD request going to the Administration and sends only the draft id", async () => {
    signIn(HOD_USER);
    const draft = { ...HOD_LEAVE, status: "draft" as const, submitted_at: null };
    const calls = mockApi((url) => {
      if (url.endsWith("/health")) return { body: HEALTH };
      if (url.endsWith("/requests/prepare")) return { body: { outcome: "draft_ready", message: "I prepared your HOD Leave.", request: draft, options: [], interpretation: {}, live_ai: false } };
      if (url.endsWith("/requests")) return { body: HOD_LEAVE };
      return undefined;
    });
    render(withProviders(<PermissionAgentPanel role="hod" />));
    await userEvent.click(screen.getByRole("button", { name: "I need leave tomorrow." }));
    const card = await screen.findByRole("generic", { name: "Request preview" });
    expect(within(card).getByText("the Administration")).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: /Confirm & Send/ }));
    const send = calls.find((c) => c.url.endsWith("/requests") && c.init?.method === "POST")!;
    expect(JSON.parse(String(send.init?.body))).toEqual({ request_id: "REQ-HOD00001" });
  });
});
