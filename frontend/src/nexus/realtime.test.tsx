import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BargeInDetector } from "@/nexus/audio/vad";
import { RECENT_WAKE_MS, autonomousStatus } from "@/nexus/lib/activity";
import { resetPreferenceCache } from "@/nexus/state/preferences";
import { STUDENT, mockApi, renderApp, signIn } from "@/test/utils";
import type { AutonomousMission } from "@/types/api";

afterEach(() => {
  vi.unstubAllGlobals();
  resetPreferenceCache();
});

const HEALTH = { ready: true, llm: { provider: "mock", live: false, model: null }, connectivity: "online" };
const NOW = Date.parse("2026-10-09T10:00:00Z");

function mission(partial: Partial<AutonomousMission>): AutonomousMission {
  return {
    mission_id: 5, agent_key: "exam_guardian", agent_label: "Exam Guardian", kind: "guardian", status: "waiting_event",
    waiting_for: "EXAM_ATTENDANCE_MARKED", next_wake_at: "2026-10-09T10:30:00Z", step_count: 2,
    created_at: "2026-10-09T09:00:00Z", updated_at: "2026-10-09T09:58:00Z", completed_at: null, result_code: null,
    last_wake_at: null, last_wake_trigger: null,
    subject: { type: "exam", title: "Demo Quiz", course_code: "CS303", due_at: "2026-10-10T10:03:00Z" },
    progress: { scope: "self", total: null, resolved: null, pending: null, own_status: "pending" }, activity: [],
    ...partial,
  };
}

describe("barge-in detection", () => {
  it("interrupts only after sustained speech over the reply", () => {
    const detector = new BargeInDetector({ minLevel: 0.05, minMs: 280 });
    expect(detector.push(0.2, 100)).toBe(false);
    expect(detector.push(0.2, 100)).toBe(false);
    expect(detector.push(0.2, 100)).toBe(true);
  });

  it("ignores residual echo and short clicks", () => {
    const detector = new BargeInDetector({ minLevel: 0.05, minMs: 280 });
    for (let i = 0; i < 20; i++) expect(detector.push(0.03, 100)).toBe(false);
    expect(detector.push(0.3, 100)).toBe(false);
    expect(detector.push(0.0, 100)).toBe(false);
    expect(detector.push(0.3, 100)).toBe(false);
    expect(detector.push(0.0, 300)).toBe(false);
    expect(detector.push(0.3, 100)).toBe(false);
  });
});

describe("autonomous mission status", () => {
  it("shows a waiting guardian with its next checkpoint", () => {
    expect(autonomousStatus(mission({}), NOW)).toMatchObject({ headline: "Waiting", tone: "amber" });
    expect(autonomousStatus(mission({}), NOW).detail).toMatch(/^Next check /);
  });

  it("shows a recent checkpoint wake-up, then falls back to the mission state", () => {
    const woke = mission({ last_wake_at: "2026-10-09T09:59:00Z", last_wake_trigger: "checkpoint" });
    expect(autonomousStatus(woke, NOW).headline).toBe("Woke at checkpoint");
    expect(autonomousStatus(woke, NOW + RECENT_WAKE_MS).headline).toBe("Waiting");
  });

  it("says a reminder was delivered only when the job reports it", () => {
    const comm = mission({ agent_key: "communication_agent", agent_label: "Communication Agent", kind: "communication",
      status: "completed", subject: { type: "communication", title: "Exam exam reminder", course_code: null, due_at: null },
      progress: { scope: "self", total: null, resolved: null, pending: null, own_status: "delivered" } });
    expect(autonomousStatus(comm, NOW)).toMatchObject({ headline: "Reminder delivered", tone: "signal" });
    const pending = { ...comm, status: "running" as const, progress: { ...comm.progress!, own_status: "in_progress" } };
    expect(autonomousStatus(pending, NOW).headline).toBe("Delivering");
    const failed = { ...comm, status: "failed" as const, progress: { ...comm.progress!, own_status: "failed" } };
    expect(autonomousStatus(failed, NOW)).toMatchObject({ headline: "Not delivered", tone: "rose" });
  });
});

describe("Nexus live context", () => {
  it("answers a greeting without a mission and never fetches a trail for it", async () => {
    signIn();
    const calls = mockApi((url, init) => {
      if (url.endsWith("/agentos/assistant/message") && init?.method === "POST") {
        return { body: { mission_id: null, status: "completed", assistant_message: "Hi! I'm Nexus.", waiting_for: null,
          steps_performed: 0, error_code: null, brain: null, route: "greeting" } };
      }
      if (url.endsWith("/auth/me")) return { body: STUDENT };
      if (url.endsWith("/health")) return { body: HEALTH };
      if (url.includes("/agentos/autonomous-missions")) return { body: { items: [], next_before_id: null } };
      return { body: [] };
    });
    renderApp("/nexus");
    await userEvent.type(await screen.findByLabelText("Message Nexus"), "hi{Enter}");
    expect(await screen.findByText("Hi! I'm Nexus.")).toBeInTheDocument();
    expect(calls.some((c) => /\/agentos\/missions\/.+\/steps/.test(c.url))).toBe(false);
  });

  it("shows Edge AI when the deployment is offline and lists autonomous agents", async () => {
    signIn();
    mockApi((url) => {
      if (url.endsWith("/auth/me")) return { body: STUDENT };
      if (url.endsWith("/health")) return { body: { ...HEALTH, connectivity: "offline" } };
      if (url.includes("/agentos/autonomous-missions")) {
        return { body: { items: [mission({ last_wake_at: new Date(Date.now() - 30_000).toISOString(), last_wake_trigger: "checkpoint" })], next_before_id: null } };
      }
      return { body: [] };
    });
    renderApp("/nexus");
    expect((await screen.findAllByText(/Edge AI · Offline/)).length).toBeGreaterThan(0);
    const rails = await screen.findAllByRole("complementary", { name: "Live activity" });
    expect(await within(rails[0]).findByText("Woke at checkpoint")).toBeInTheDocument();
    expect(within(rails[0]).getAllByText("Exam Guardian").length).toBeGreaterThan(0);
  });
});
