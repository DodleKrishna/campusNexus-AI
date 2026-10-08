import { AudioLines, Lock, Plus, ShieldCheck, Sparkles } from "lucide-react";
import { useRef, useState } from "react";
import { useAuth } from "@/auth/useAuth";
import { Composer, type ComposerHandle } from "@/nexus/components/Composer";
import { NexusOrb } from "@/nexus/components/NexusOrb";
import { NxButton, Pill } from "@/nexus/components/primitives";
import { Transcript } from "@/nexus/components/Transcript";
import { AiModeBadge } from "@/nexus/layout/ActivityRail";
import { useShell } from "@/nexus/layout/ShellContext";
import { friendlyName } from "@/nexus/lib/names";
import { suggestionsFor } from "@/nexus/lib/suggestions";
import { useAssistant } from "@/nexus/state/useAssistant";
import { cn } from "@/utils/cn";
import { formatLongDate, greeting } from "@/utils/format";

function SuggestionChips({ items, onPick, className, delay = 0 }: { items: string[]; onPick: (text: string) => void; className?: string; delay?: number }) {
  if (items.length === 0) return null;
  return (
    <ul className={cn("flex flex-wrap justify-center gap-2", className)} aria-label="Suggestions">
      {items.map((text, i) => (
        <li key={text} className="animate-rise-sm" style={{ animationDelay: `${delay + i * 60}ms` }}>
          <button
            type="button"
            onClick={() => onPick(text)}
            className="group flex cursor-pointer items-center gap-2 rounded-full border border-line bg-white/[0.025] px-3.5 py-2 text-[13px] text-mist transition-all duration-200 hover:-translate-y-0.5 hover:border-cyan/35 hover:bg-cyan/[0.06] hover:text-frost hover:shadow-[0_8px_30px_-12px_rgb(34_211_238/0.5)]"
          >
            <Sparkles className="size-3.5 text-cyan/70 transition-colors group-hover:text-cyan" />
            {text}
          </button>
        </li>
      ))}
    </ul>
  );
}

export function AssistantPage() {
  const { user } = useAuth();
  const { turns, busy, send, draft, setDraft, reset } = useAssistant();
  const { openVoice } = useShell();
  const composer = useRef<ComposerHandle>(null);
  const [now] = useState(() => new Date());
  if (!user) return null;

  const suggestions = suggestionsFor(user.role);
  const pick = (text: string) => void send(text);
  const name = friendlyName(user.display_name);

  if (turns.length === 0) {
    return (
      <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col items-center justify-center px-5 py-10 sm:px-8">
        <div className="flex items-center gap-2 animate-rise" style={{ animationDelay: "0ms" }}>
          <Pill tone="cyan" className="px-3 py-1">
            <Sparkles className="size-3" /> Nexus · your campus agent
          </Pill>
          <AiModeBadge />
        </div>

        <h1 className="mt-6 text-center font-display text-4xl leading-[1.08] font-bold tracking-tight text-balance text-frost animate-rise sm:text-5xl" style={{ animationDelay: "80ms" }}>
          {greeting(now)}, <span className="nx-text-gradient">{name}</span>
        </h1>
        <p className="mt-3 text-center text-[15px] text-haze animate-rise" style={{ animationDelay: "160ms" }}>
          {formatLongDate(now)} · What should we get done today?
        </p>

        <button
          type="button"
          onClick={openVoice}
          aria-label="Talk to Nexus"
          className="group relative my-4 w-[min(68vw,320px)] cursor-pointer rounded-full animate-fade sm:my-6"
          style={{ animationDelay: "200ms" }}
        >
          <NexusOrb state="idle" className="w-full transition-transform duration-700 group-hover:scale-[1.04]" />
          <span className="absolute inset-x-0 bottom-[6%] flex justify-center opacity-0 transition-opacity duration-300 group-hover:opacity-100 group-focus-visible:opacity-100">
            <span className="flex items-center gap-1.5 rounded-full bg-void/70 px-3 py-1 text-xs text-frost ring-1 ring-line-strong backdrop-blur">
              <AudioLines className="size-3.5 text-cyan" /> Tap to talk
            </span>
          </span>
        </button>

        <div className="w-full animate-rise" style={{ animationDelay: "300ms" }}>
          <Composer ref={composer} value={draft} onChange={setDraft} onSubmit={(v) => void send(v)} onVoice={openVoice} busy={busy} />
        </div>
        <SuggestionChips items={suggestions.slice(0, 4)} onPick={pick} className="mt-5" delay={420} />

        <p className="mt-10 flex items-center gap-2 text-center text-xs text-dim animate-fade" style={{ animationDelay: "700ms" }}>
          <ShieldCheck className="size-3.5" />
          Nexus reads and plans. Anything sensitive waits for a human — and every step is audited.
        </p>
      </div>
    );
  }

  const last = turns[turns.length - 1];
  return (
    <div className="flex min-h-full flex-1 flex-col">
      <header className="sticky top-0 z-10 border-b border-line bg-void/55 backdrop-blur-xl">
        <div className="mx-auto flex w-full max-w-3xl items-center gap-3 px-5 py-3 sm:px-8 md:pr-20 xl:pr-8">
          <button type="button" onClick={openVoice} aria-label="Talk to Nexus" className="-my-2 w-14 shrink-0 cursor-pointer rounded-full">
            <NexusOrb state={busy ? "thinking" : "idle"} className="w-full" />
          </button>
          <div className="min-w-0 flex-1">
            <p className="font-display text-[15px] font-semibold text-frost">Nexus</p>
            <p className="truncate text-xs text-haze" aria-live="polite">
              {busy ? <span className="nx-shimmer-text">Coordinating agents…</span> : last?.error ? "Something went wrong — try again" : "Ready"}
            </p>
          </div>
          <AiModeBadge />
          <NxButton
            size="sm"
            onClick={() => {
              reset();
              composer.current?.focus();
            }}
            disabled={busy}
          >
            <Plus /> New
          </NxButton>
        </div>
      </header>

      <div className="mx-auto w-full max-w-3xl flex-1 px-5 pt-8 pb-6 sm:px-8">
        <Transcript turns={turns} />
      </div>

      <div className="sticky bottom-0 z-10 bg-gradient-to-t from-void via-void/90 to-transparent pt-6 pb-4 sm:pb-6">
        <div className="mx-auto w-full max-w-3xl px-5 sm:px-8">
          {!busy && <SuggestionChips items={suggestions.slice(0, 3)} onPick={pick} className="mb-3 justify-start" />}
          <Composer ref={composer} value={draft} onChange={setDraft} onSubmit={(v) => void send(v)} onVoice={openVoice} busy={busy} placeholder="Ask a follow-up…" />
          <p className="mt-2 flex items-center justify-center gap-1.5 text-[11px] text-dim">
            <Lock className="size-3" /> Answers come from your campus records and cited policy.
          </p>
        </div>
      </div>
    </div>
  );
}
