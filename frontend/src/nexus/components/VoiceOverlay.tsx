import { ArrowUp, Keyboard, MessageSquareText, Mic, MicOff, PhoneOff, RotateCcw, X } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { CampusLogo } from "@/nexus/components/brand";
import { NexusOrb, type OrbState } from "@/nexus/components/NexusOrb";
import { NxButton, Pill, StatusDot, type Tone } from "@/nexus/components/primitives";
import { Transcript } from "@/nexus/components/Transcript";
import { useVoiceSession, type VoiceState } from "@/nexus/hooks/useVoiceSession";
import { AmbientBackground } from "@/nexus/layout/AmbientBackground";
import { usePreferences } from "@/nexus/state/preferences";
import { useAssistant } from "@/nexus/state/useAssistant";
import { cn } from "@/utils/cn";

type Display = VoiceState | "muted" | "capturing";

const COPY: Record<Display, { title: string; hint: string; tone: Tone; pill: string }> = {
  idle: { title: "Ready when you are", hint: "Tap the orb to start talking.", tone: "dim", pill: "Idle" },
  connecting: { title: "Connecting…", hint: "Waiting for microphone access.", tone: "cyan", pill: "Connecting" },
  listening: { title: "Listening", hint: "Go ahead — I'm listening.", tone: "cyan", pill: "Listening" },
  capturing: { title: "Listening…", hint: "Pause when you're done, or tap the orb to send.", tone: "cyan", pill: "Listening" },
  muted: { title: "Microphone muted", hint: "Unmute to keep talking.", tone: "dim", pill: "Muted" },
  thinking: { title: "Thinking…", hint: "Nexus is coordinating your agents.", tone: "violet", pill: "Thinking" },
  speaking: { title: "Speaking", hint: "Tap the orb to interrupt.", tone: "signal", pill: "Speaking" },
  error: { title: "Voice paused", hint: "", tone: "rose", pill: "Needs attention" },
};

function ControlButton({ label, onClick, active, danger, children, disabled }: { label: string; onClick: () => void; active?: boolean; danger?: boolean; children: React.ReactNode; disabled?: boolean }) {
  return (
    <div className="flex flex-col items-center gap-1.5">
      <button
        type="button"
        onClick={onClick}
        disabled={disabled}
        aria-label={label}
        aria-pressed={active}
        className={cn(
          "flex size-14 cursor-pointer items-center justify-center rounded-full transition-all duration-200 disabled:opacity-40 [&_svg]:size-[22px]",
          danger
            ? "bg-rose text-white shadow-[0_10px_40px_-8px_rgb(251_113_133/0.7)] hover:brightness-110 active:scale-95"
            : active
              ? "bg-frost text-void hover:bg-white active:scale-95"
              : "nx-glass text-frost hover:bg-glass-strong active:scale-95",
        )}
      >
        {children}
      </button>
      <span className="text-[11px] text-haze">{label}</span>
    </div>
  );
}

/**
 * The full-screen voice conversation. Mounting it opens the microphone;
 * closing it ends the session (and aborts anything in flight).
 */
export function VoiceOverlay({ onClose }: { onClose: () => void }) {
  const assistant = useAssistant();
  const prefs = usePreferences();
  const voice = useVoiceSession({ onUtterance: assistant.sendVoice, speakReplies: prefs.speakReplies });
  const [firstTurn] = useState(() => assistant.turns.length);
  const [typing, setTyping] = useState(false);
  const [showTranscript, setShowTranscript] = useState(() => window.matchMedia?.("(min-width: 1024px)").matches ?? false);
  const [text, setText] = useState("");
  const panel = useRef<HTMLDivElement>(null);
  const textInput = useRef<HTMLInputElement>(null);
  const titleId = useId();
  const { start, end } = voice;

  const close = useCallback(() => {
    end();
    onClose();
  }, [end, onClose]);
  useFocusTrap(panel, true, close);

  useEffect(() => {
    void start();
    return () => end();
  }, [start, end]);

  useEffect(() => {
    if (typing) textInput.current?.focus();
  }, [typing]);

  const sessionTurns = assistant.turns.slice(firstTurn);
  const textBusy = assistant.busy && voice.state === "listening";
  const display: Display =
    textBusy ? "thinking" : voice.state === "listening" ? (voice.muted ? "muted" : voice.capturing ? "capturing" : "listening") : voice.state;
  const copy = COPY[display];
  const orbState: OrbState = display === "capturing" ? "listening" : display === "muted" ? "muted" : (display as OrbState);

  const onOrb = () => {
    if (voice.state === "speaking") voice.interrupt();
    else if (voice.state === "listening" && voice.capturing) voice.sendNow();
    else if (voice.state === "error" || voice.state === "idle") void voice.retry();
  };

  const sendText = (event: FormEvent) => {
    event.preventDefault();
    if (!text.trim() || assistant.busy) return;
    void assistant.send(text);
    setText("");
  };

  return (
    <div ref={panel} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1} className="nx fixed inset-0 z-50 flex flex-col overflow-hidden animate-fade focus:outline-none">
      <AmbientBackground intensity="strong" className="absolute" />
      <div className="absolute inset-0 bg-void/70 backdrop-blur-2xl" aria-hidden />

      <header className="relative z-10 flex items-center justify-between px-4 pt-4 sm:px-8 sm:pt-6">
        <CampusLogo className="hidden sm:flex" />
        <CampusLogo compact className="sm:hidden" />
        <h2 id={titleId} className="sr-only">
          Voice session with Nexus
        </h2>
        <Pill tone={copy.tone} className="px-3 py-1 text-xs">
          <StatusDot tone={copy.tone === "dim" ? "dim" : copy.tone} live={["listening", "capturing", "speaking", "thinking", "connecting"].includes(display)} />
          {copy.pill}
        </Pill>
        <NxButton variant="ghost" size="icon" onClick={close} aria-label="Close voice session">
          <X />
        </NxButton>
      </header>

      <div className="relative z-10 flex min-h-0 flex-1">
        <main className="flex min-w-0 flex-1 flex-col items-center justify-center px-6">
          <button
            type="button"
            onClick={onOrb}
            aria-label={voice.state === "speaking" ? "Interrupt Nexus" : voice.capturing ? "Send now" : voice.state === "error" ? "Try again" : "Nexus"}
            className="group relative w-[min(78vw,46vh,520px)] cursor-pointer rounded-full focus-visible:outline-offset-8"
          >
            <NexusOrb state={orbState} analyser={voice.analyser} className="w-full transition-transform duration-500 group-active:scale-[0.97]" />
          </button>
          <div className="mt-2 min-h-24 text-center" aria-live="polite">
            <p key={display} className="font-display text-3xl font-semibold tracking-tight text-frost animate-rise-sm sm:text-4xl">
              {copy.title}
            </p>
            <p className="mx-auto mt-2 max-w-md text-[15px] text-haze">{display === "error" ? voice.error : copy.hint}</p>
            {display === "error" && (
              <div className="mt-4 flex justify-center gap-2">
                <NxButton size="sm" onClick={() => void voice.retry()}>
                  <RotateCcw /> Try again
                </NxButton>
                <NxButton size="sm" onClick={() => setTyping(true)}>
                  <Keyboard /> Type instead
                </NxButton>
              </div>
            )}
          </div>
        </main>

        <aside
          aria-label="Session transcript"
          className={cn(
            "nx-glass nx-scroll absolute inset-x-3 bottom-3 max-h-[45%] overflow-y-auto rounded-3xl p-5 lg:static lg:m-6 lg:ml-0 lg:max-h-none lg:w-[400px] lg:shrink-0",
            showTranscript ? "block animate-rise-sm" : "hidden",
          )}
        >
          <div className="mb-4 flex items-center justify-between">
            <p className="text-[11px] font-semibold tracking-[0.14em] text-haze uppercase">Transcript</p>
            <span className="text-[11px] text-dim">This session</span>
          </div>
          {sessionTurns.length === 0 ? (
            <p className="text-sm text-haze">Your conversation appears here. Spoken requests show as voice messages — the recording is transcribed on the server and never stored.</p>
          ) : (
            <Transcript turns={sessionTurns} size="sm" />
          )}
        </aside>
      </div>

      <footer className="relative z-10 px-4 pb-[max(env(safe-area-inset-bottom),1.25rem)] sm:pb-8">
        {typing && (
          <form onSubmit={sendText} className="nx-glass nx-ring mx-auto mb-5 flex max-w-xl items-center gap-2 rounded-2xl p-1.5 pl-4 animate-rise-sm">
            <label htmlFor="voice-text" className="sr-only">
              Type a message to Nexus
            </label>
            <input
              id="voice-text"
              ref={textInput}
              value={text}
              maxLength={1000}
              onChange={(e) => setText(e.target.value)}
              placeholder="Type instead…"
              className="h-10 flex-1 bg-transparent text-sm text-frost placeholder:text-dim focus:outline-none"
            />
            <NxButton type="submit" variant="primary" size="icon" aria-label="Send" disabled={!text.trim() || assistant.busy}>
              <ArrowUp />
            </NxButton>
          </form>
        )}
        <div className="mx-auto flex max-w-md items-end justify-center gap-5 sm:gap-7">
          <ControlButton label={voice.muted ? "Unmute" : "Mute"} onClick={voice.toggleMute} active={voice.muted} disabled={voice.state === "idle" || voice.state === "connecting" || voice.state === "error"}>
            {voice.muted ? <MicOff /> : <Mic />}
          </ControlButton>
          <ControlButton label="Type" onClick={() => setTyping((v) => !v)} active={typing}>
            <Keyboard />
          </ControlButton>
          <ControlButton label="Transcript" onClick={() => setShowTranscript((v) => !v)} active={showTranscript}>
            <MessageSquareText />
          </ControlButton>
          <ControlButton label="End" onClick={close} danger>
            <PhoneOff />
          </ControlButton>
        </div>
      </footer>
    </div>
  );
}
