import { X } from "lucide-react";
import { useId, useRef, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { cn } from "@/utils/cn";

/** A centred modal. Focus is trapped inside; Escape and a backdrop click close it. */
export function Dialog({ open, onClose, title, children, className }: { open: boolean; onClose: () => void; title: string; children: ReactNode; className?: string }) {
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useFocusTrap(panel, open, onClose);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-ink/35 p-0 animate-fade-in sm:items-center sm:p-4" onMouseDown={onClose}>
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={cn(
          "max-h-[90dvh] w-full overflow-y-auto rounded-t-card border border-border bg-surface shadow-[var(--shadow-popover)] animate-pop-in focus:outline-none sm:max-w-md sm:rounded-card",
          className,
        )}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-3 border-b border-border px-5 py-3.5">
          <h2 id={titleId} className="text-sm font-semibold">
            {title}
          </h2>
          <Button variant="ghost" size="icon-sm" aria-label="Close" onClick={onClose}>
            <X />
          </Button>
        </div>
        <div className="px-5 py-4">{children}</div>
      </div>
    </div>
  );
}
