import { useEffect, useRef, type RefObject } from "react";

const FOCUSABLE = 'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * While ``active``: move focus into the container, keep Tab / Shift+Tab inside it,
 * close on Escape, and give focus back to whatever had it before on release.
 */
export function useFocusTrap(ref: RefObject<HTMLElement | null>, active: boolean, onEscape: () => void) {
  // Callers pass inline callbacks; keep the latest without re-running the trap (which would steal focus).
  const escape = useRef(onEscape);
  useEffect(() => {
    escape.current = onEscape;
  });

  useEffect(() => {
    const container = ref.current;
    if (!active || !container) return;
    const previous = document.activeElement as HTMLElement | null;
    const first = container.querySelector<HTMLElement>("[data-autofocus]") ?? container;
    first.focus();

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        escape.current();
        return;
      }
      if (event.key !== "Tab") return;
      const all = [...container.querySelectorAll<HTMLElement>(FOCUSABLE)];
      const visible = all.filter((el) => el.offsetParent !== null || el === document.activeElement);
      const items = visible.length ? visible : all;
      if (items.length === 0) {
        event.preventDefault();
        return;
      }
      const head = items[0];
      const tail = items[items.length - 1];
      if (event.shiftKey && (document.activeElement === head || document.activeElement === container)) {
        event.preventDefault();
        tail.focus();
      } else if (!event.shiftKey && document.activeElement === tail) {
        event.preventDefault();
        head.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previous?.focus?.();
    };
  }, [ref, active]);
}
