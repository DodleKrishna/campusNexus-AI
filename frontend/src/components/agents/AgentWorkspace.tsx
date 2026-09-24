import { ArrowUp, Loader2, RotateCcw } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ResponseRenderer } from "@/components/agents/ResponseRenderer";
import { ModeBadge } from "@/components/layout/ModeBadge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Textarea } from "@/components/ui/input";
import { ErrorState } from "@/components/ui/states";
import type { ChatScope } from "@/api/endpoints";
import type { AgentDefinition } from "@/features/agents/catalog";
import { useAgentChat } from "@/hooks/useAgentChat";

/**
 * The one chat workspace every agent uses: header, suggested questions,
 * conversation, composer, and a structured renderer per agent.
 */
export function AgentWorkspace({
  agent,
  userId,
  initialQuestion,
  onInitialQuestionSent,
  scope = "student",
}: {
  agent: AgentDefinition;
  scope?: ChatScope;
  userId: number;
  initialQuestion?: string;
  onInitialQuestionSent?: () => void;
}) {
  const { turns, send, clear, isSending } = useAgentChat(agent.key, userId, scope);
  const [draft, setDraft] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  // A question handed over from "Ask CampusNexus" is sent once, then consumed.
  const handedOver = useRef<string | null>(null);
  useEffect(() => {
    if (!initialQuestion) {
      handedOver.current = null;
      return;
    }
    if (isSending || handedOver.current === initialQuestion) return;
    handedOver.current = initialQuestion;
    send(initialQuestion);
    onInitialQuestionSent?.();
  }, [initialQuestion, isSending, send, onInitialQuestionSent]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns]);

  const submit = () => {
    if (!draft.trim()) return;
    send(draft);
    setDraft("");
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <Card className="flex items-start gap-4 px-5 py-4">
        <div className="flex size-11 shrink-0 items-center justify-center rounded-lg bg-accent-soft text-accent-hover">
          <agent.icon className="size-5" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-lg font-semibold">{agent.name}</h1>
            <Badge tone="success">Available</Badge>
            {(agent.key === "enquiry" || scope === "faculty") && <Badge tone="info">Read-only</Badge>}
          </div>
          <p className="mt-0.5 text-sm text-muted">{agent.responsibility}</p>
        </div>
        <div className="flex items-center gap-2">
          <ModeBadge />
          {turns.length > 0 && (
            <Button variant="ghost" size="sm" onClick={clear}>
              <RotateCcw /> New conversation
            </Button>
          )}
        </div>
      </Card>

      <Card className="flex min-h-[420px] flex-col">
        <div className="flex-1 space-y-6 px-5 py-5" aria-live="polite">
          {turns.length === 0 && (
            <div className="mx-auto max-w-xl py-8 text-center">
              <p className="text-sm font-medium">Ask {agent.name} a question</p>
              <p className="mt-1 text-xs text-muted">
                {scope === "faculty"
                  ? "Answers use only your own classes and the requests routed to you. Every count is computed from attendance records."
                  : "Answers use your own records. Every figure is computed by CampusNexus's rules and verified before you see it."}
              </p>
            </div>
          )}
          {turns.map((turn) => (
            <div key={turn.id} className="space-y-3">
              <div className="flex justify-end">
                <div className="max-w-[80%] rounded-2xl rounded-br-md bg-primary px-4 py-2 text-sm text-primary-foreground">{turn.question}</div>
              </div>
              <div className="max-w-[92%]">
                {turn.pending && (
                  <div className="flex items-center gap-2 text-sm text-muted" role="status">
                    <Loader2 className="size-4 animate-spin text-accent" />
                    {agent.key === "enquiry" && scope === "student" ? "Consulting the specialist agents…" : `${agent.name} is checking the records…`}
                  </div>
                )}
                {turn.error && <ErrorState message={turn.error} />}
                {turn.response && <ResponseRenderer response={turn.response} />}
              </div>
            </div>
          ))}
          <div ref={endRef} />
        </div>

        <div className="border-t border-border px-5 py-4">
          {agent.suggestions.length > 0 && (
            <div className="mb-3 flex flex-wrap gap-2">
              {agent.suggestions.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  disabled={isSending}
                  onClick={() => send(suggestion)}
                  className="rounded-full border border-border bg-surface px-3 py-1 text-xs text-muted transition-colors hover:border-accent/40 hover:text-ink disabled:opacity-50"
                >
                  {suggestion}
                </button>
              ))}
            </div>
          )}
          <div className="flex items-end gap-2">
            <Textarea
              rows={1}
              value={draft}
              maxLength={1000}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onKeyDown}
              placeholder={`Message ${agent.name}…`}
              aria-label={`Message ${agent.name}`}
              className="max-h-40 min-h-10 resize-none"
            />
            <Button variant="accent" size="icon" aria-label="Send" onClick={submit} disabled={!draft.trim() || isSending}>
              {isSending ? <Loader2 className="animate-spin" /> : <ArrowUp />}
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}
