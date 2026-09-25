import { useId, useRef, type KeyboardEvent, type ReactNode } from "react";
import { cn } from "@/utils/cn";

export interface TabItem {
  id: string;
  label: string;
  count?: number;
}

/**
 * Underline tabs with roving focus (arrow keys). The panel is rendered by the caller
 * for the selected tab; wrap it in <TabPanel> so it is labelled by its tab.
 */
export function Tabs({ tabs, value, onChange, className, label }: { tabs: TabItem[]; value: string; onChange: (id: string) => void; className?: string; label: string }) {
  const base = useId();
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (event: KeyboardEvent, index: number) => {
    const delta = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!delta) return;
    event.preventDefault();
    const next = (index + delta + tabs.length) % tabs.length;
    onChange(tabs[next].id);
    refs.current[next]?.focus();
  };
  return (
    <div role="tablist" aria-label={label} className={cn("-mx-1 flex gap-1 overflow-x-auto border-b border-border px-1", className)}>
      {tabs.map((tab, index) => {
        const selected = tab.id === value;
        return (
          <button
            key={tab.id}
            ref={(el) => {
              refs.current[index] = el;
            }}
            type="button"
            role="tab"
            id={`${base}-${tab.id}`}
            aria-selected={selected}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(tab.id)}
            onKeyDown={(event) => onKey(event, index)}
            className={cn(
              "-mb-px inline-flex shrink-0 items-center gap-2 border-b-2 px-3 py-2.5 text-sm font-medium transition-colors",
              selected ? "border-accent text-ink" : "border-transparent text-muted hover:text-ink",
            )}
          >
            {tab.label}
            {tab.count !== undefined && (
              <span className={cn("rounded-full px-1.5 text-xs tabular-nums", selected ? "bg-accent-soft text-accent-hover" : "bg-surface-muted text-muted")}>{tab.count}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

export function TabPanel({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div role="tabpanel" className={cn("pt-5", className)}>
      {children}
    </div>
  );
}
