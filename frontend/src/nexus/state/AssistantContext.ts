import { createContext } from "react";
import type { TrailItem } from "@/nexus/lib/activity";
import type { AgentMissionStatus, BrainInfo, VoiceAssistantReply } from "@/types/api";

export interface ChatTurn {
  id: string;
  role: "user" | "nexus";
  via: "text" | "voice";
  /** Empty for a voice turn from the user: the server never returns the transcript. */
  text: string;
  /** Length of a spoken user turn, in seconds. */
  durationSec?: number;
  createdAt: number;
  pending?: boolean;
  missionId?: number;
  status?: AgentMissionStatus;
  trail?: TrailItem[];
  trailState?: "loading" | "ready" | "unavailable";
  error?: string;
  /** A short note about the spoken reply (e.g. voice output unavailable). */
  note?: string;
}

/** One entry of the session's live agent-activity feed (the right rail). */
export interface ActivityEvent extends TrailItem {
  missionId: number;
  at: number;
}

export type VoiceTurnResult = { ok: true; reply: VoiceAssistantReply } | { ok: false; error: string; code?: number };

export interface AssistantContextValue {
  turns: ChatTurn[];
  busy: boolean;
  brain: BrainInfo | null;
  activity: ActivityEvent[];
  send: (text: string) => Promise<void>;
  sendVoice: (wav: Blob, durationSec: number, signal?: AbortSignal) => Promise<VoiceTurnResult>;
  /** Pre-fills the composer (used by suggestion chips and the Agents page). */
  draft: string;
  setDraft: (text: string) => void;
  reset: () => void;
}

export const AssistantContext = createContext<AssistantContextValue | null>(null);
