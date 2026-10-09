import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo, useRef, useState, type ReactNode } from "react";
import { ApiError } from "@/api/client";
import { api, queryKeys } from "@/api/endpoints";
import { missionTrail } from "@/nexus/lib/activity";
import { AssistantContext, type ActivityEvent, type AssistantContextValue, type ChatTurn, type VoiceTurnResult } from "@/nexus/state/AssistantContext";
import type { AssistantReply, BrainInfo } from "@/types/api";

const MAX_ACTIVITY = 24;

/** What Nexus says when the mission ended without a message of its own. */
function fallbackText(reply: AssistantReply): string {
  if (reply.status === "waiting_human") return "I need a little more information to continue.";
  if (reply.status === "failed" || reply.status === "cancelled") return "I couldn't complete that request. Nothing was executed.";
  if (reply.status === "completed") return "Done.";
  return "I'm still working on that. You can follow it under Missions.";
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "Nexus is not reachable right now. Please try again.";
}

/**
 * The Nexus conversation for this browser session. Text and voice turns both go
 * through here; after each reply the mission's persisted steps are fetched to
 * build the activity trail (structured records only, never reasoning).
 */
export function AssistantProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [busy, setBusy] = useState(false);
  const [brain, setBrain] = useState<BrainInfo | null>(null);
  const [activity, setActivity] = useState<ActivityEvent[]>([]);
  const [draft, setDraft] = useState("");
  const counter = useRef(0);

  const nextId = useCallback((prefix: string) => `${prefix}-${++counter.current}`, []);
  const patch = useCallback((id: string, change: Partial<ChatTurn>) => {
    setTurns((all) => all.map((t) => (t.id === id ? { ...t, ...change } : t)));
  }, []);

  const loadTrail = useCallback(
    async (turnId: string, missionId: number) => {
      patch(turnId, { trailState: "loading" });
      try {
        const steps = await queryClient.fetchQuery({ queryKey: queryKeys.missionSteps(missionId), queryFn: () => api.missionSteps(missionId), staleTime: 0 });
        const trail = missionTrail(steps);
        patch(turnId, { trail, trailState: "ready" });
        const at = Date.now();
        setActivity((all) => [...trail.slice(1).reverse().map((item, i) => ({ ...item, key: `${missionId}-${item.key}`, missionId, at: at - i })), ...all].slice(0, MAX_ACTIVITY));
      } catch {
        patch(turnId, { trailState: "unavailable" });
      }
    },
    [patch, queryClient],
  );

  const settle = useCallback(
    (turnId: string, reply: AssistantReply, note?: string) => {
      if (reply.brain) setBrain(reply.brain);
      patch(turnId, {
        pending: false, text: reply.assistant_message?.trim() || fallbackText(reply), missionId: reply.mission_id ?? undefined, status: reply.status, note,
      });
      if (reply.mission_id == null) return; // answered by the ingress in code: no mission, no trail
      void queryClient.invalidateQueries({ queryKey: queryKeys.assistantMissions });
      void loadTrail(turnId, reply.mission_id);
    },
    [loadTrail, patch, queryClient],
  );

  const send = useCallback(
    async (raw: string) => {
      const text = raw.trim().slice(0, 1000);
      if (!text || busy) return;
      const replyId = nextId("nexus");
      setBusy(true);
      setDraft("");
      setTurns((all) => [
        ...all,
        { id: nextId("user"), role: "user", via: "text", text, createdAt: Date.now() },
        { id: replyId, role: "nexus", via: "text", text: "", createdAt: Date.now(), pending: true },
      ]);
      try {
        settle(replyId, await api.assistantMessage(text));
      } catch (error) {
        patch(replyId, { pending: false, error: errorText(error) });
      } finally {
        setBusy(false);
      }
    },
    [busy, nextId, patch, settle],
  );

  const sendVoice = useCallback(
    async (wav: Blob, durationSec: number, signal?: AbortSignal): Promise<VoiceTurnResult> => {
      const replyId = nextId("nexus");
      setBusy(true);
      setTurns((all) => [
        ...all,
        { id: nextId("user"), role: "user", via: "voice", text: "", durationSec, createdAt: Date.now() },
        { id: replyId, role: "nexus", via: "voice", text: "", createdAt: Date.now(), pending: true },
      ]);
      try {
        const reply = await api.assistantVoice(wav, true, signal);
        settle(replyId, reply, reply.audio_wav_base64 ? undefined : "Spoken reply unavailable — shown as text.");
        return { ok: true, reply };
      } catch (error) {
        if ((error as Error).name === "AbortError") {
          patch(replyId, { pending: false, error: "Voice session ended before Nexus replied." });
          return { ok: false, error: "aborted" };
        }
        const message = errorText(error);
        patch(replyId, { pending: false, error: message });
        return { ok: false, error: message, code: error instanceof ApiError ? error.status : undefined };
      } finally {
        setBusy(false);
      }
    },
    [nextId, patch, settle],
  );

  const reset = useCallback(() => {
    setTurns([]);
    setDraft("");
  }, []);

  const value = useMemo<AssistantContextValue>(
    () => ({ turns, busy, brain, activity, send, sendVoice, draft, setDraft, reset }),
    [turns, busy, brain, activity, send, sendVoice, draft, reset],
  );
  return <AssistantContext.Provider value={value}>{children}</AssistantContext.Provider>;
}
