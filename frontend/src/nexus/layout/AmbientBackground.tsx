import { cn } from "@/utils/cn";

/** The living backdrop behind every CAMPUS AI surface: grid, drifting light, film grain. */
export function AmbientBackground({ className, intensity = "normal" }: { className?: string; intensity?: "normal" | "strong" }) {
  return (
    <div className={cn("pointer-events-none fixed inset-0 overflow-hidden", className)} aria-hidden>
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_80%_60%_at_50%_-10%,rgb(30_58_138/0.35),transparent_70%)]" />
      <div
        className={cn(
          "absolute -top-[20%] -left-[15%] size-[60vmax] rounded-full bg-[radial-gradient(circle,rgb(34_211_238/0.16),transparent_60%)] blur-3xl animate-drift",
          intensity === "strong" && "bg-[radial-gradient(circle,rgb(34_211_238/0.24),transparent_60%)]",
        )}
      />
      <div
        className={cn(
          "absolute -right-[20%] -bottom-[25%] size-[65vmax] rounded-full bg-[radial-gradient(circle,rgb(139_92_246/0.16),transparent_60%)] blur-3xl animate-drift-slow",
          intensity === "strong" && "bg-[radial-gradient(circle,rgb(139_92_246/0.24),transparent_60%)]",
        )}
      />
      <div className="nx-grid absolute inset-0" />
      <div className="nx-noise absolute inset-0" />
    </div>
  );
}
