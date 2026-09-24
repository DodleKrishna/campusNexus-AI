import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentWorkspace } from "@/components/agents/AgentWorkspace";
import { agentByKey } from "@/features/agents/catalog";
import { mockApi, signIn, withProviders } from "@/test/utils";
import type { AgentQueryResponse } from "@/types/api";

const ATTENDANCE_ANSWER: AgentQueryResponse = {
  agent_key: "academic",
  display_name: "Academic Agent",
  verification_status: "verified",
  answer: "Your attendance in Operating Systems (CS301) is 68.0%.",
  facts: {
    intent: "attendance_status",
    course_resolution: { status: "resolved", course_code: "CS301" },
    attendance: { status: "ok", classes_attended: 34, classes_conducted: 50, required_percentage: "75", current_percentage: "68.0", eligible_now: false, classes_needed_to_reach_threshold: 14 },
    threshold: { status: "ok", document_id: "attendance-policy-v2" },
  },
  evidence: [{ evidence_id: "e1", document_id: "attendance-policy-v2", title: "Attendance Policy (2025-26)", snippet: "75% minimum", section: "Minimum Attendance Requirement", policy_version: "v2", source: "x" }],
  issues: [],
  consulted: [],
  notices: [],
  action_hint: null,
  live_ai: false,
};

afterEach(() => vi.unstubAllGlobals());

describe("AgentWorkspace", () => {
  it("sends a suggested question to that agent and renders structured cards", async () => {
    signIn();
    const calls = mockApi((url) => {
      if (url.endsWith("/agents/academic/query")) return { body: ATTENDANCE_ANSWER };
      if (url.endsWith("/me/attendance")) return { body: [] };
      if (url.endsWith("/health")) return { body: { ready: true, llm: { provider: "mock", live: false, model: null } } };
      return undefined;
    });
    render(withProviders(<AgentWorkspace agent={agentByKey("academic")!} userId={1} />));

    await userEvent.click(screen.getByRole("button", { name: "What is my OS attendance?" }));

    expect(await screen.findByText("68%")).toBeInTheDocument();
    expect(screen.getByText("75%")).toBeInTheDocument();
    expect(screen.getByText("34 / 50")).toBeInTheDocument();
    expect(screen.getByText("Attend 14 consecutive classes to reach 75%.")).toBeInTheDocument();
    expect(screen.getByText(/Source: Attendance Policy \(2025-26\) v2/)).toBeInTheDocument();
    const call = calls.find((c) => c.url.endsWith("/agents/academic/query"))!;
    // Only the message is sent; identity comes from the bearer token.
    expect(JSON.parse(String(call.init?.body))).toEqual({ message: "What is my OS attendance?" });
  });

  it("shows a clean message when live AI is unavailable", async () => {
    signIn();
    mockApi((url) =>
      url.includes("/agents/") ? { status: 503, body: { detail: "Live AI is temporarily unavailable. Please try again shortly." } } : undefined,
    );
    render(withProviders(<AgentWorkspace agent={agentByKey("enquiry")!} userId={1} />));

    await userEvent.type(screen.getByLabelText("Message Enquiry Agent"), "Anything important today?{Enter}");

    expect(await screen.findByText("Live AI is temporarily unavailable. Please try again shortly.")).toBeInTheDocument();
  });
});
