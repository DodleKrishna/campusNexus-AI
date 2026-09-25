import { X } from "lucide-react";
import { useId, useRef, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { cn } from "@/utils/cn";

/**
 * A side drawer for details and the mobile navigation. Rendered only while open,
 * with focus trapped inside; Escape and a backdrop click close it.
 */
export function Sheet({
  open,
  onClose,
  title,
  description,
  side = "right",
  children,
  footer,
  className,
  hideHeader = false,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: ReactNode;
  side?: "left" | "right";
  children: ReactNode;
  footer?: ReactNode;
  className?: string;
  hideHeader?: boolean;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useFocusTrap(panel, open, onClose);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 bg-ink/35 animate-fade-in" onMouseDown={onClose}>
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
        className={cn(
          "absolute inset-y-0 flex w-full flex-col bg-surface shadow-[var(--shadow-popover)] focus:outline-none",
          side === "right" ? "right-0 max-w-md animate-sheet-right" : "left-0 max-w-[288px] animate-sheet-left",
          className,
        )}
      >
        {hideHeader ? (
          <h2 id={titleId} className="sr-only">
            {title}
          </h2>
        ) : (
          <div className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
            <div className="min-w-0">
              <h2 id={titleId} className="text-base font-semibold">
                {title}
              </h2>
              {description && <p className="mt-0.5 text-sm text-muted">{description}</p>}
            </div>
            <Button variant="ghost" size="icon-sm" aria-label="Close" onClick={onClose}>
              <X />
            </Button>
          </div>
        )}
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
        {footer && <div className="border-t border-border px-5 py-3">{footer}</div>}
      </div>
    </div>
  );
}
