import { useEffect, useRef, useState, type ReactNode } from "react";
import { cn } from "@/utils/cn";

/**
 * A click-toggled panel anchored under its trigger; closes on outside click or Escape.
 * On phones it spans the viewport width under the top bar instead of overflowing.
 */
export function Popover({
  trigger,
  children,
  align = "end",
  className,
}: {
  trigger: (props: { open: boolean; toggle: () => void; close: () => void }) => ReactNode;
  children: ReactNode | ((props: { close: () => void }) => ReactNode);
  align?: "start" | "end";
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const close = () => setOpen(false);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={root} className="relative">
      {trigger({ open, toggle: () => setOpen((value) => !value), close })}
      {open && (
        <div
          className={cn(
            "z-40 rounded-card border border-border bg-surface shadow-[var(--shadow-popover)] animate-pop-in",
            "max-sm:fixed max-sm:inset-x-3 max-sm:top-16 max-sm:w-auto",
            "sm:absolute sm:top-full sm:mt-2",
            align === "end" ? "sm:right-0" : "sm:left-0",
            className,
          )}
        >
          {typeof children === "function" ? children({ close }) : children}
        </div>
      )}
    </div>
  );
}
