import { ArrowUp, Loader2, RotateCcw } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { AgentHeader, ThinkingIndicator } from "@/components/agents/AgentHeader";
import { ResponseRenderer } from "@/components/agents/ResponseRenderer";
import { VerificationBadge } from "@/components/agents/VerificationBadge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/input";
import { ErrorState } from "@/components/ui/states";
import type { ChatScope } from "@/api/endpoints";
import type { AgentDefinition } from "@/features/agents/catalog";
import { useAgentChat } from "@/hooks/useAgentChat";

const SCOPE_NOTE: Record<ChatScope, string> = {
  student: "Answers use your own records. Every figure is computed by campus rules and verified before you see it.",
  faculty: "Answers use only your own classes and the requests routed to you, computed from attendance records.",
  hod: "Answers use only your department's classes, students, requests and complaints, computed from records.",
  admin: "Answers cover the whole institution, from records. Every count is computed, never estimated.",
};

/**
 * The one workspace every agent uses: header, suggested prompts, conversation with
 * structured result cards, and a composer that stays at the bottom of the screen.
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
  const title = agent.title ?? agent.name;
  const enquiry = agent.key === "enquiry";

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
    if (turns.length) endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
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

  const chips = (
    <div className="flex flex-wrap gap-1.5">
      {agent.suggestions.map((suggestion) => (
        <button
          key={suggestion}
          type="button"
          disabled={isSending}
          onClick={() => send(suggestion)}
          className="rounded-md border border-border bg-surface px-2.5 py-1 text-[13px] text-ink transition-colors hover:border-primary/40 hover:bg-primary-soft/50 disabled:opacity-50"
        >
          {suggestion}
        </button>
      ))}
    </div>
  );

  return (
    <div className="mx-auto flex w-full max-w-[800px] flex-1 flex-col">
      <AgentHeader
        icon={agent.icon}
        title={title}
        tagline={agent.tagline}
        capability={enquiry || scope !== "student" ? "Read-only" : undefined}
        action={
          turns.length > 0 && (
            <Button variant="ghost" size="sm" onClick={clear}>
              <RotateCcw /> <span className="hidden sm:inline">New conversation</span>
            </Button>
          )
        }
      />

      <div className="flex-1 space-y-8 py-6" aria-live="polite">
        {turns.length === 0 && (
          <div className="py-2">
            <p className="text-sm text-muted">{SCOPE_NOTE[scope]}</p>
            {agent.suggestions.length > 0 && (
              <div className="mt-4">
                <p className="mb-2 text-xs font-medium tracking-wide text-muted uppercase">Suggested</p>
                {chips}
              </div>
            )}
          </div>
        )}

        {turns.map((turn) => (
          <div key={turn.id} className="space-y-3">
            <div className="flex justify-end">
              <div className="max-w-[80%] rounded-lg rounded-br-sm bg-primary px-3.5 py-2 text-sm text-primary-foreground">{turn.question}</div>
            </div>
            <div className="min-w-0 space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <agent.icon aria-hidden className="size-4 text-primary" />
                <span className="text-[13px] font-medium text-ink">{enquiry ? title : (turn.response?.display_name ?? title)}</span>
                {turn.response && <VerificationBadge status={turn.response.verification_status} />}
              </div>
              {turn.pending && <ThinkingIndicator label={enquiry && scope === "student" ? "Checking campus services…" : "Checking the records…"} />}
              {turn.error && <ErrorState message={turn.error} />}
              {turn.response && <ResponseRenderer response={turn.response} scope={scope} />}
            </div>
          </div>
        ))}
        <div ref={endRef} />
      </div>

      <div className="sticky bottom-0 z-10 -mx-4 bg-background px-4 pt-2 pb-[max(1rem,env(safe-area-inset-bottom))] sm:mx-0 sm:px-0">
        {turns.length > 0 && agent.suggestions.length > 0 && <div className="mb-2 max-h-16 overflow-y-auto">{chips}</div>}
        <div className="flex items-end gap-2 rounded-card border border-border bg-surface p-1.5 shadow-[var(--shadow-raised)] transition-colors focus-within:border-primary focus-within:ring-3 focus-within:ring-primary/15">
          <Textarea
            rows={1}
            value={draft}
            maxLength={1000}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder={`Ask ${title}…`}
            aria-label={`Message ${agent.name}`}
            className="field-sizing-content max-h-40 min-h-9 resize-none border-0 bg-transparent py-2 shadow-none hover:border-0 focus:ring-0"
          />
          <Button size="icon" aria-label="Send" onClick={submit} disabled={!draft.trim() || isSending}>
            {isSending ? <Loader2 className="animate-spin" /> : <ArrowUp />}
          </Button>
        </div>
        <p className="mt-1.5 hidden text-center text-xs text-subtle sm:block">Enter to send · Shift + Enter for a new line</p>
      </div>
    </div>
  );
}
