import { useSyncExternalStore } from "react";

/** Subscribe to a CSS media query. Without matchMedia (tests, SSR) it reports ``fallback``. */
export function useMediaQuery(query: string, fallback = true): boolean {
  return useSyncExternalStore(
    (notify) => {
      if (typeof window === "undefined" || !window.matchMedia) return () => {};
      const list = window.matchMedia(query);
      list.addEventListener("change", notify);
      return () => list.removeEventListener("change", notify);
    },
    () => (typeof window !== "undefined" && window.matchMedia ? window.matchMedia(query).matches : fallback),
    () => fallback,
  );
}
