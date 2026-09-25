import { BrandMark } from "@/components/ui/brand";

/**
 * The application bootstrap loader, shown only while the session is being checked.
 * ``stage`` names the real step in progress; nothing waits on a timer. Pages inside
 * the shell use skeletons instead.
 */
export function AppLoader({ stage = "Checking your session" }: { stage?: string }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-background animate-fade-in" role="status" aria-live="polite">
      <div className="flex w-56 flex-col items-center">
        <BrandMark className="size-10" />
        <div className="mt-6 h-0.5 w-full overflow-hidden rounded-full bg-border" aria-hidden>
          <div className="h-full w-2/5 rounded-full bg-primary animate-loader-line" />
        </div>
        <p className="mt-4 text-sm font-medium text-ink">Preparing your workspace</p>
        <p className="mt-0.5 text-xs text-muted">{stage}</p>
      </div>
    </div>
  );
}
