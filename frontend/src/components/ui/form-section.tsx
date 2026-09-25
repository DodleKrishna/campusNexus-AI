import type { ReactNode } from "react";

/** A settings-style section: title and description on the left, content on the right. */
export function FormSection({ title, description, children }: { title: string; description?: string; children: ReactNode }) {
  return (
    <section className="grid grid-cols-1 gap-4 border-t border-border py-6 first:border-t-0 first:pt-0 md:grid-cols-[14rem_minmax(0,1fr)] md:gap-8">
      <div>
        <h2 className="text-sm font-semibold text-ink">{title}</h2>
        {description && <p className="mt-1 text-[13px] text-muted">{description}</p>}
      </div>
      <div className="min-w-0 rounded-card border border-border bg-surface px-5 py-1">{children}</div>
    </section>
  );
}
