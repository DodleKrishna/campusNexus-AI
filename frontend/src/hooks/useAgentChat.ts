import { useMutation } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/api/client";
import { api, type ChatScope } from "@/api/endpoints";
import type { AgentQueryResponse } from "@/types/api";

export interface ChatTurn {
  id: string;
  question: string;
  response?: AgentQueryResponse;
  error?: string;
  pending?: boolean;
}

const storageKey = (userId: number, agentKey: string, scope: ChatScope) =>
  scope === "student" ? `campusnexus.chat.${userId}.${agentKey}` : `campusnexus.chat.${scope}.${userId}.${agentKey}`;

function load(key: string): ChatTurn[] {
  try {
    const raw = window.sessionStorage.getItem(key);
    const turns = raw ? (JSON.parse(raw) as ChatTurn[]) : [];
    return turns.filter((t) => !t.pending);
  } catch {
    return [];
  }
}

/** One agent conversation. Kept for the browser session (per user and agent) so it survives navigation. */
export function useAgentChat(agentKey: string, userId: number, scope: ChatScope = "student") {
  const key = storageKey(userId, agentKey, scope);
  const [turns, setTurns] = useState<ChatTurn[]>(() => load(key));

  useEffect(() => {
    try {
      window.sessionStorage.setItem(key, JSON.stringify(turns.filter((t) => !t.pending).slice(-30)));
    } catch {
      /* ignore */
    }
  }, [key, turns]);

  const mutation = useMutation({
    mutationFn: ({ message }: { id: string; message: string }) => api.askAgent(agentKey, message, scope),
    onSuccess: (response, { id }) => setTurns((all) => all.map((t) => (t.id === id ? { ...t, response, pending: false } : t))),
    onError: (error, { id }) =>
      setTurns((all) =>
        all.map((t) =>
          t.id === id
            ? { ...t, pending: false, error: error instanceof ApiError ? error.message : "CampusNexus couldn't reach this agent." }
            : t,
        ),
      ),
  });

  const send = useCallback(
    (message: string) => {
      const question = message.trim();
      if (!question || mutation.isPending) return;
      const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      setTurns((all) => [...all, { id, question, pending: true }]);
      mutation.mutate({ id, message: question });
    },
    [mutation],
  );

  const clear = useCallback(() => setTurns([]), []);
  return { turns, send, clear, isSending: mutation.isPending };
}
