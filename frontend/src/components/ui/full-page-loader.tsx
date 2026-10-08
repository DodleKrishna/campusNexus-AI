import { CampusMark } from "@/nexus/components/brand";

/**
 * The application bootstrap loader, shown only while the session is being checked
 * or a workspace chunk is loading. ``stage`` names the real step in progress;
 * nothing waits on a timer. Pages inside the shell use skeletons instead.
 */
export function AppLoader({ stage = "Checking your session" }: { stage?: string }) {
  return (
    <div className="nx fixed inset-0 z-50 flex items-center justify-center animate-fade-in" role="status" aria-live="polite">
      <div className="flex w-56 flex-col items-center">
        <div className="relative">
          <div className="absolute inset-0 rounded-full bg-cyan/30 blur-2xl" aria-hidden />
          <CampusMark className="relative size-12" animated />
        </div>
        <div className="mt-7 h-px w-full overflow-hidden rounded-full bg-line-strong" aria-hidden>
          <div className="h-full w-1/3 rounded-full bg-gradient-to-r from-transparent via-cyan to-transparent animate-scan" />
        </div>
        <p className="mt-4 text-sm font-medium text-frost">Preparing CAMPUS AI</p>
        <p className="mt-0.5 text-xs text-haze">{stage}</p>
      </div>
    </div>
  );
}
