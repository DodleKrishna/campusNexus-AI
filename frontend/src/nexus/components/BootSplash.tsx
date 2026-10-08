import { useEffect, useState } from "react";
import { useAuth } from "@/auth/useAuth";
import { PRODUCT_FULL_NAME, PRODUCT_TAGLINE } from "@/nexus/components/brand";
import { AmbientBackground } from "@/nexus/layout/AmbientBackground";
import { cn } from "@/utils/cn";

const BOOT_KEY = "campusai.booted";

function minimumMs(): number {
  try {
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return 500;
    // The full intro once per browser session; a quick one on later reloads.
    return window.sessionStorage.getItem(BOOT_KEY) ? 900 : 2600;
  } catch {
    return 1200;
  }
}

function OrbitMark() {
  return (
    <div className="relative size-36" aria-hidden>
      <div className="absolute inset-0 rounded-full bg-cyan/25 blur-3xl animate-pulse" />
      <svg viewBox="0 0 160 160" className="absolute inset-0 animate-spin-slow" style={{ animationDuration: "18s" }}>
        <circle cx="80" cy="80" r="74" fill="none" stroke="url(#bs-g)" strokeOpacity=".5" strokeWidth="1" strokeDasharray="2 7" />
        <circle cx="80" cy="6" r="3.5" fill="#22d3ee" />
        <defs>
          <linearGradient id="bs-g" x1="0" y1="0" x2="160" y2="160" gradientUnits="userSpaceOnUse">
            <stop stopColor="#22d3ee" />
            <stop offset="1" stopColor="#8b5cf6" />
          </linearGradient>
        </defs>
      </svg>
      <svg viewBox="0 0 160 160" className="absolute inset-0 animate-spin-slow" style={{ animationDuration: "9s", animationDirection: "reverse" }}>
        <circle cx="80" cy="80" r="54" fill="none" stroke="#3b82f6" strokeOpacity=".35" strokeWidth="1.2" />
        <circle cx="134" cy="80" r="3" fill="#8b5cf6" />
        <circle cx="26" cy="80" r="2.5" fill="#3b82f6" />
      </svg>
      <svg viewBox="0 0 160 160" className="absolute inset-0 animate-spin-slow" style={{ animationDuration: "5s" }}>
        <path d="M80 44 111 98H49Z" fill="none" stroke="url(#bs-g2)" strokeWidth="1.6" strokeLinejoin="round" />
        <defs>
          <linearGradient id="bs-g2" x1="49" y1="44" x2="111" y2="98" gradientUnits="userSpaceOnUse">
            <stop stopColor="#22d3ee" />
            <stop offset=".6" stopColor="#3b82f6" />
            <stop offset="1" stopColor="#8b5cf6" />
          </linearGradient>
        </defs>
      </svg>
      <div className="absolute inset-[38%] rounded-full bg-[radial-gradient(circle_at_35%_30%,#e0f7ff,#22d3ee_45%,#3b82f6)] shadow-[0_0_40px_rgb(34_211_238/0.8)]" />
    </div>
  );
}

/**
 * The full-screen intro on app start. It stays until the session check has
 * finished *and* a short minimum has passed, then dissolves; a click or Escape
 * skips it. The stage line names the real step in progress.
 */
export function BootSplash() {
  const { status } = useAuth();
  const [minimum] = useState(minimumMs);
  const [minDone, setMinDone] = useState(false);
  const [skipped, setSkipped] = useState(false);
  const [gone, setGone] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => setMinDone(true), minimum);
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && setSkipped(true);
    window.addEventListener("keydown", onKey);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("keydown", onKey);
    };
  }, [minimum]);

  const leaving = (minDone || skipped) && status !== "loading";
  useEffect(() => {
    if (!leaving) return;
    try {
      window.sessionStorage.setItem(BOOT_KEY, "1");
    } catch {
      // storage blocked: the full intro simply plays again next time
    }
    // Backstop in case the fade's transitionend never fires.
    const timer = window.setTimeout(() => setGone(true), 1000);
    return () => window.clearTimeout(timer);
  }, [leaving]);

  if (gone) return null;
  const stage = status === "loading" ? "Checking your session" : leaving ? "Ready" : "Waking your agents";

  return (
    <div
      className={cn("nx fixed inset-0 z-[100] flex flex-col items-center justify-center overflow-hidden transition-[opacity,filter] duration-700 ease-out", leaving && "pointer-events-none opacity-0 blur-sm")}
      onClick={() => setSkipped(true)}
      onTransitionEnd={(event) => event.target === event.currentTarget && leaving && setGone(true)}
      role="status"
      aria-live="polite"
      aria-label={`CAMPUS AI is loading: ${stage}`}
    >
      <AmbientBackground intensity="strong" className="absolute" />
      <div className="relative flex flex-col items-center px-6 text-center">
        <div className="animate-fade">
          <OrbitMark />
        </div>
        <h1 className="mt-10 flex font-display text-5xl font-extrabold tracking-[0.18em] text-frost sm:text-6xl" aria-label="CAMPUS AI">
          {"CAMPUS".split("").map((ch, i) => (
            <span key={i} className="animate-rise" style={{ animationDelay: `${250 + i * 70}ms` }} aria-hidden>
              {ch}
            </span>
          ))}
          <span className="w-[0.45em]" aria-hidden />
          {"AI".split("").map((ch, i) => (
            <span key={`ai${i}`} className="nx-text-gradient animate-rise" style={{ animationDelay: `${750 + i * 90}ms` }} aria-hidden>
              {ch}
            </span>
          ))}
        </h1>
        <p className="mt-4 max-w-md text-[11px] font-medium tracking-[0.22em] text-haze uppercase animate-fade sm:text-xs" style={{ animationDelay: "1050ms" }}>
          {PRODUCT_FULL_NAME}
        </p>
        <p className="mt-6 font-display text-lg font-medium text-mist animate-rise sm:text-xl" style={{ animationDelay: "1350ms" }}>
          {PRODUCT_TAGLINE}
        </p>
      </div>
      <div className="absolute bottom-14 flex w-56 flex-col items-center animate-fade" style={{ animationDelay: "400ms" }}>
        <div className="h-px w-full overflow-hidden rounded-full bg-line-strong">
          <div className="h-full w-1/3 rounded-full bg-gradient-to-r from-transparent via-cyan to-transparent animate-scan" />
        </div>
        <p className="mt-3 text-[11px] tracking-[0.16em] text-haze uppercase">{stage}</p>
      </div>
    </div>
  );
}
