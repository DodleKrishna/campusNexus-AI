import { Loader2 } from "lucide-react";

export function FullPageLoader({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex h-full min-h-[60vh] items-center justify-center" role="status">
      <div className="flex items-center gap-3 text-sm text-muted">
        <Loader2 className="size-4 animate-spin text-accent" />
        {label}
      </div>
    </div>
  );
}
