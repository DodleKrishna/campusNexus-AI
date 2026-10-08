import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { isRouteForRole } from "@/auth/roles";
import { DEFAULT_VAD, VoiceActivityDetector } from "@/nexus/audio/vad";
import { encodeWav, resample } from "@/nexus/audio/wav";
import { missionTrail, stepToTrail } from "@/nexus/lib/activity";
import { friendlyName } from "@/nexus/lib/names";
import { resetPreferenceCache } from "@/nexus/state/preferences";
import { FACULTY, STUDENT, mockApi, renderApp, signIn } from "@/test/utils";
import type { AgentStepView, AssistantMissionSummary } from "@/types/api";

afterEach(() => {
  vi.unstubAllGlobals();
  resetPreferenceCache();
});

const HEALTH = { ready: true, llm: { provider: "mock", live: false, model: null } };

function step(partial: Partial<AgentStepView> & Pick<AgentStepView, "id" | "step_number" | "action_type">): AgentStepView {
  return {
    agent_key: "nexus_orchestrator", tool_name: null, delegated_agent: null, input_summary: {}, output_summary: {},
    status: "executed", latency_ms: null, created_at: "2026-10-08T10:00:00Z", ...partial,
  };
}

const CONSULT = step({
  id: 11, step_number: 1, action_type: "tool", tool_name: "consult_domain_specialist", latency_ms: 140,
  input_summary: { kind: "tool", tool_name: "consult_domain_specialist", tool_input: { specialist: "academic", question: "PRIVATE QUESTION TEXT" } },
  output_summary: { ok: true, data: { verification_status: "verified", answer: "PRIVATE ANSWER TEXT", sources: [{ document_id: "a" }, { document_id: "b" }] } },
});
const COMPLETE = step({ id: 12, step_number: 2, action_type: "complete", input_summary: { kind: "complete", user_message: "PRIVATE MESSAGE" }, output_summary: { status: "completed" } });

describe("activity trail mapping", () => {
  it("names the specialist and shows only enumerated status fields", () => {
    const item = stepToTrail(CONSULT);
    expect(item.actor).toBe("Academic Agent");
    expect(item.detail).toBe("Verified · 2 sources");
    expect(JSON.stringify(missionTrail([COMPLETE, CONSULT]))).not.toMatch(/PRIVATE/);
  });

  it("orders steps after the intake and ends with the response", () => {
    expect(missionTrail([COMPLETE, CONSULT]).map((i) => i.label)).toEqual(["Analyzed your request", "Consulted with evidence", "Response generated"]);
  });

  it("reports a rejected decision as blocked by the safety checks", () => {
    const item = stepToTrail(step({ id: 3, step_number: 1, action_type: "tool", tool_name: "anything", status: "rejected" }));
    expect(item).toMatchObject({ actor: "Safety checks", tone: "rose" });
  });

  it("never claims success for a failed tool", () => {
    const item = stepToTrail(step({ id: 4, step_number: 1, action_type: "tool", tool_name: "get_my_exams", status: "failed" }));
    expect(item.label).toMatch(/unavailable/);
    expect(item.tone).toBe("amber");
  });
});

describe("voice activity detection", () => {
  const chunk = 100;

  it("ends an utterance exactly at the configured silence", () => {
    const vad = new VoiceActivityDetector();
    expect(vad.push(0.2, chunk)).toBe("speech_start");
    for (let i = 0; i < 4; i++) expect(vad.push(0.2, chunk)).toBe("none");
    const silent = DEFAULT_VAD.endSilenceMs / chunk;
    for (let i = 0; i < silent - 1; i++) expect(vad.push(0, chunk)).toBe("none");
    expect(vad.push(0, chunk)).toBe("utterance_end");
    expect(vad.inUtterance).toBe(false);
  });

  it("discards a blip shorter than the minimum speech", () => {
    const vad = new VoiceActivityDetector({ minSpeechMs: 300, endSilenceMs: 200 });
    expect(vad.push(0.2, chunk)).toBe("speech_start");
    expect(vad.push(0, chunk)).toBe("none");
    expect(vad.push(0, chunk)).toBe("discard");
  });

  it("caps an utterance at the maximum length", () => {
    const vad = new VoiceActivityDetector({ maxUtteranceMs: 500 });
    vad.push(0.2, chunk);
    const events = Array.from({ length: 4 }, () => vad.push(0.2, chunk));
    expect(events).toEqual(["none", "none", "none", "max_length"]);
  });

  it("ignores room noise below the threshold", () => {
    const vad = new VoiceActivityDetector();
    for (let i = 0; i < 50; i++) expect(vad.push(0.004, chunk)).toBe("none");
  });
});

describe("wav encoding", () => {
  it("writes a 16 kHz mono 16-bit PCM header", () => {
    const view = new DataView(encodeWav(new Float32Array([0, 0.5, -0.5, 1])));
    const text = (offset: number) => String.fromCharCode(...Array.from({ length: 4 }, (_, i) => view.getUint8(offset + i)));
    expect(text(0)).toBe("RIFF");
    expect(text(8)).toBe("WAVE");
    expect(view.getUint16(22, true)).toBe(1);
    expect(view.getUint32(24, true)).toBe(16_000);
    expect(view.getUint16(34, true)).toBe(16);
    expect(view.getUint32(40, true)).toBe(8);
    expect(view.getInt16(44 + 6, true)).toBe(0x7fff);
  });

  it("downsamples 48 kHz to 16 kHz", () => {
    expect(resample(new Float32Array(4800).fill(0.25), 48_000).length).toBe(1600);
    expect(resample(new Float32Array(4800).fill(0.25), 48_000)[10]).toBeCloseTo(0.25);
  });
});

describe("helpers", () => {
  it("greets people by a friendly name", () => {
    expect(friendlyName("Aditi Rao")).toBe("Aditi");
    expect(friendlyName("Dr. Ashok Verma")).toBe("Dr. Verma");
  });

  it("only returns someone to a page that belongs to their role", () => {
    expect(isRouteForRole("student", "/student/requests")).toBe(true);
    expect(isRouteForRole("student", "/nexus/missions")).toBe(true);
    expect(isRouteForRole("student", "/admin")).toBe(false);
    expect(isRouteForRole("faculty", "/facultyish")).toBe(false);
  });
});

const MISSION: AssistantMissionSummary = {
  mission_id: 42, status: "completed", goal: "Am I eligible to write my OS exam?", assistant_message: "Yes — your attendance is 82%, above the 75% requirement.",
  waiting_for: null, step_count: 2, created_at: "2026-10-08T10:00:00Z", updated_at: "2026-10-08T10:00:05Z",
};

function nexusApi(extra?: (url: string, init?: RequestInit) => { status?: number; body?: unknown } | undefined) {
  return mockApi((url, init) => {
    const custom = extra?.(url, init);
    if (custom) return custom;
    if (url.endsWith("/auth/me")) return { body: STUDENT };
    if (url.endsWith("/health")) return { body: HEALTH };
    if (url.includes("/agentos/assistant/missions")) return { body: [] };
    if (url.includes("/requests") || url.includes("/me/")) return { body: [] };
    return undefined;
  });
}

describe("Nexus assistant", () => {
  it("greets the student and shows role suggestions", async () => {
    signIn();
    nexusApi();
    renderApp("/nexus");
    expect(await screen.findByRole("heading", { name: /, Aditi$/ })).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Suggestions" })).getAllByRole("button").length).toBeGreaterThan(0);
    expect(await screen.findAllByText("Offline mock")).not.toHaveLength(0);
  });

  it("sends a message, shows the reply and the structured agent trail", async () => {
    signIn();
    const calls = nexusApi((url, init) => {
      if (url.endsWith("/agentos/assistant/message") && init?.method === "POST") {
        return { body: { mission_id: 42, status: "completed", assistant_message: MISSION.assistant_message, waiting_for: null, steps_performed: 2, error_code: null, brain: { provider: "mock", model: null, live: false } } };
      }
      if (url.endsWith("/agentos/missions/42/steps")) return { body: [CONSULT, COMPLETE] };
      return undefined;
    });
    renderApp("/nexus");
    await userEvent.type(await screen.findByLabelText("Message Nexus"), "Am I eligible to write my OS exam?{Enter}");

    expect(await screen.findByText(MISSION.assistant_message!)).toBeInTheDocument();
    const post = calls.find((c) => c.url.endsWith("/agentos/assistant/message"));
    expect(JSON.parse(String(post?.init?.body))).toEqual({ message: "Am I eligible to write my OS exam?" });

    const trail = (await screen.findAllByRole("list", { name: "Agent activity" }))[0];
    expect(await within(trail).findByText("Academic Agent")).toBeInTheDocument();
    expect(within(trail).getByText("Verified · 2 sources")).toBeInTheDocument();
    expect(within(trail).getByText(/Response generated/)).toBeInTheDocument();
    expect(screen.queryByText(/PRIVATE/)).not.toBeInTheDocument();
  });

  it("shows the server's message when the assistant is unavailable", async () => {
    signIn();
    nexusApi((url) =>
      url.endsWith("/agentos/assistant/message")
        ? { status: 503, body: { detail: { code: "PROVIDER_UNAVAILABLE", message: "The assistant's AI provider is unavailable right now. Nothing was executed." } } }
        : undefined,
    );
    renderApp("/nexus");
    await userEvent.type(await screen.findByLabelText("Message Nexus"), "hello{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("Nothing was executed. (PROVIDER_UNAVAILABLE)");
  });

  it("opens a voice session and explains when no microphone is available", async () => {
    signIn();
    nexusApi();
    renderApp("/nexus");
    await userEvent.click(await screen.findByRole("button", { name: "Start a voice conversation" }));
    const dialog = await screen.findByRole("dialog", { name: "Voice session with Nexus" });
    expect(await within(dialog).findByText(/Voice needs a microphone/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Type" })).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "End" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Voice session with Nexus" })).not.toBeInTheDocument());
  });

  it("lists missions and expands one into its agent activity", async () => {
    signIn(FACULTY);
    mockApi((url) => {
      if (url.endsWith("/auth/me")) return { body: FACULTY };
      if (url.endsWith("/health")) return { body: HEALTH };
      if (url.includes("/agentos/assistant/missions")) return { body: [MISSION] };
      if (url.endsWith("/agentos/missions/42/steps")) return { body: [CONSULT, COMPLETE] };
      return { body: [] };
    });
    renderApp("/nexus/missions");
    const row = await screen.findByRole("button", { name: /Am I eligible to write my OS exam\?/ });
    await userEvent.click(row);
    expect(await screen.findByText(MISSION.assistant_message!)).toBeInTheDocument();
    const trails = await screen.findAllByRole("list", { name: "Agent activity" });
    expect(await within(trails[trails.length - 1]).findByText("Academic Agent")).toBeInTheDocument();
  });
});
