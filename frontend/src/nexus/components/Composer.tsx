import { ArrowUp, AudioLines, Loader2 } from "lucide-react";
import { forwardRef, useImperativeHandle, useRef, type FormEvent, type KeyboardEvent } from "react";
import { NxButton } from "@/nexus/components/primitives";
import { cn } from "@/utils/cn";

export interface ComposerHandle {
  focus: () => void;
}

/**
 * The text box under the orb: auto-growing input, a send button and the voice
 * button. Enter sends, Shift+Enter adds a line.
 */
export const Composer = forwardRef<ComposerHandle, {
  value: string;
  onChange: (value: string) => void;
  onSubmit: (value: string) => void;
  onVoice?: () => void;
  busy?: boolean;
  placeholder?: string;
  className?: string;
}>(function Composer({ value, onChange, onSubmit, onVoice, busy = false, placeholder = "Ask Nexus anything…", className }, ref) {
  const input = useRef<HTMLTextAreaElement>(null);
  useImperativeHandle(ref, () => ({ focus: () => input.current?.focus() }), []);

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (!value.trim() || busy) return;
    onSubmit(value);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  return (
    <form onSubmit={submit} className={cn("group nx-glass nx-ring relative flex items-end gap-2 rounded-[22px] p-2 pl-4 shadow-[0_20px_60px_-20px_rgb(34_211_238/0.25)] transition-shadow focus-within:shadow-[0_20px_70px_-15px_rgb(59_130_246/0.45)]", className)}>
      <label htmlFor="nexus-input" className="sr-only">
        Message Nexus
      </label>
      <textarea
        id="nexus-input"
        ref={input}
        rows={1}
        value={value}
        maxLength={1000}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={onKeyDown}
        placeholder={placeholder}
        className="field-sizing-content max-h-40 min-h-11 flex-1 resize-none bg-transparent py-2.5 text-[15px] text-frost placeholder:text-dim focus:outline-none"
      />
      {onVoice && (
        <NxButton variant="ghost" size="icon" onClick={onVoice} aria-label="Start a voice conversation" title="Talk to Nexus" className="h-11 w-11 rounded-2xl text-cyan hover:text-cyan">
          <AudioLines />
        </NxButton>
      )}
      <NxButton type="submit" variant="primary" size="icon" disabled={!value.trim() || busy} aria-label="Send" className="h-11 w-11 rounded-2xl">
        {busy ? <Loader2 className="animate-spin" /> : <ArrowUp />}
      </NxButton>
    </form>
  );
});
