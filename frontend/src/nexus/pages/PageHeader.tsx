import type { ReactNode } from "react";

/** The calm page header used by every secondary CAMPUS AI page. */
export function PageHeader({ eyebrow, title, description, action }: { eyebrow: string; title: string; description?: string; action?: ReactNode }) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-4 animate-rise">
      <div className="min-w-0">
        <p className="text-[11px] font-semibold tracking-[0.16em] text-cyan/80 uppercase">{eyebrow}</p>
        <h1 className="mt-2 font-display text-3xl font-bold tracking-tight text-frost sm:text-4xl">{title}</h1>
        {description && <p className="mt-2 max-w-xl text-[15px] text-haze">{description}</p>}
      </div>
      {action}
    </header>
  );
}
