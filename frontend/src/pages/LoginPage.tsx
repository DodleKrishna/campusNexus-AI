import { ArrowRight, Eye, EyeOff, Loader2, LockKeyhole, ScrollText, ShieldCheck, Workflow } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { ApiError } from "@/api/client";
import { homeRouteFor, isRouteForRole } from "@/auth/roles";
import { useAuth } from "@/auth/useAuth";
import { CampusLogo, PRODUCT_FULL_NAME } from "@/nexus/components/brand";
import { NexusOrb } from "@/nexus/components/NexusOrb";
import { NxButton } from "@/nexus/components/primitives";
import { AmbientBackground } from "@/nexus/layout/AmbientBackground";

/** Optional institution name, configured per deployment (never hard-coded). */
const INSTITUTION = (import.meta.env.VITE_INSTITUTION_NAME as string | undefined)?.trim();

const AGENTS = ["Academic", "Career", "Events", "Campus Services", "Assignment Guardian", "Exam Guardian", "Attendance Guardian", "Knowledge"];

const PILLARS = [
  { icon: Workflow, title: "Agents that act", text: "Nexus plans and coordinates specialists toward your goal." },
  { icon: ShieldCheck, title: "Rules, not guesses", text: "Eligibility and deadlines are computed by deterministic code." },
  { icon: ScrollText, title: "Humans in control", text: "Sensitive actions wait for approval. Everything is audited." },
];

const fieldClass =
  "h-12 w-full rounded-xl border border-line-strong bg-white/[0.03] px-4 text-[15px] text-frost transition-all placeholder:text-dim hover:border-white/20 focus:border-cyan/60 focus:bg-white/[0.05] focus:shadow-[0_0_0_4px_rgb(34_211_238/0.12)] focus:outline-none";

export function LoginPage() {
  const { status, user, login, notice } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [reveal, setReveal] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [showHelp, setShowHelp] = useState(false);

  if (status === "authenticated" && user) return <Navigate to={homeRouteFor(user.role)} replace />;

  const from = (location.state as { from?: string } | null)?.from;
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const signedIn = await login(email.trim(), password);
      navigate(from && isRouteForRole(signedIn.role, from) ? from : homeRouteFor(signedIn.role), { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Sign-in failed. Please try again.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="nx relative flex min-h-dvh flex-col overflow-x-hidden">
      <AmbientBackground intensity="strong" />

      <header className="relative z-10 flex items-center justify-between px-5 py-5 sm:px-10 animate-fade">
        <CampusLogo />
        <span className="hidden items-center gap-1.5 text-xs text-haze sm:flex">
          <LockKeyhole className="size-3.5" /> Secure role-based access
        </span>
      </header>

      <main className="relative z-10 mx-auto grid w-full max-w-7xl flex-1 items-center gap-12 px-5 pb-12 sm:px-10 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)] lg:gap-8">
        <section className="order-2 min-w-0 lg:order-1">
          <p className="inline-flex max-w-full items-center gap-2 rounded-full border border-line bg-white/[0.03] px-3 py-1.5 text-[11px] font-medium tracking-[0.08em] text-mist uppercase animate-rise">
            <span className="size-1.5 shrink-0 rounded-full bg-cyan shadow-[0_0_8px_#22d3ee]" />
            <span className="truncate">{PRODUCT_FULL_NAME}</span>
          </p>
          <h1 className="mt-6 font-display text-[2.6rem] leading-[1.02] font-extrabold tracking-tight text-frost sm:text-6xl xl:text-7xl">
            <span className="block animate-rise" style={{ animationDelay: "80ms" }}>One Campus.</span>
            <span className="block animate-rise" style={{ animationDelay: "180ms" }}>Many Agents.</span>
            <span className="nx-text-gradient block pb-2 animate-rise" style={{ animationDelay: "280ms" }}>One Intelligence.</span>
          </h1>
          <p className="mt-5 max-w-lg text-base leading-relaxed text-haze sm:text-lg animate-rise" style={{ animationDelay: "380ms" }}>
            Tell Nexus what you need — by voice or text. A network of campus agents plans it, checks it against official rules, and gets it done with you in control.
          </p>

          <ul className="mt-9 grid gap-4 sm:grid-cols-3">
            {PILLARS.map((p, i) => (
              <li key={p.title} className="animate-rise" style={{ animationDelay: `${480 + i * 90}ms` }}>
                <p.icon className="size-5 text-cyan" />
                <p className="mt-2.5 text-sm font-semibold text-frost">{p.title}</p>
                <p className="mt-1 text-[13px] leading-relaxed text-haze">{p.text}</p>
              </li>
            ))}
          </ul>

          <div className="relative mt-10 overflow-hidden [mask-image:linear-gradient(90deg,transparent,#000_12%,#000_88%,transparent)] animate-fade" style={{ animationDelay: "800ms" }} aria-hidden>
            <div className="flex w-max gap-2 animate-marquee">
              {[...AGENTS, ...AGENTS].map((name, i) => (
                <span key={i} className="rounded-full border border-line bg-white/[0.025] px-3 py-1 text-xs whitespace-nowrap text-mist">
                  {name}
                </span>
              ))}
            </div>
          </div>
        </section>

        <section className="relative order-1 flex min-w-0 justify-center lg:order-2">
          <div className="pointer-events-none absolute top-1/2 left-1/2 w-[min(150vw,760px)] -translate-x-1/2 -translate-y-1/2 opacity-60 animate-fade" aria-hidden>
            <NexusOrb state="idle" className="w-full" />
          </div>
          <div className="nx-glass nx-ring relative w-full max-w-[420px] rounded-[28px] bg-abyss/75 p-7 shadow-[0_40px_120px_-30px_rgb(0_0_0/0.8)] animate-rise sm:p-9" style={{ animationDelay: "200ms" }}>
            <h2 className="font-display text-2xl font-bold tracking-tight text-frost">Welcome back</h2>
            <p className="mt-1.5 text-sm text-haze">{INSTITUTION ? `Sign in to ${INSTITUTION}` : "Sign in to meet Nexus, your campus agent."}</p>
            {notice && (
              <p role="alert" className="mt-5 rounded-xl bg-amber/10 px-3.5 py-2.5 text-sm text-amber ring-1 ring-amber/25 ring-inset">
                {notice}
              </p>
            )}
            <form className="mt-7 space-y-5" onSubmit={submit} noValidate>
              <div className="space-y-2">
                <label htmlFor="email" className="text-[13px] font-medium text-mist">
                  Email
                </label>
                <input id="email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@campusnexus.local" className={fieldClass} />
              </div>
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <label htmlFor="password" className="text-[13px] font-medium text-mist">
                    Password
                  </label>
                  <button type="button" onClick={() => setShowHelp((v) => !v)} aria-expanded={showHelp} aria-controls="password-help" className="cursor-pointer text-[13px] text-cyan/90 hover:text-cyan">
                    Forgot password?
                  </button>
                </div>
                <div className="relative">
                  <input id="password" type={reveal ? "text" : "password"} autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} className={`${fieldClass} pr-12`} />
                  <button
                    type="button"
                    onClick={() => setReveal((v) => !v)}
                    aria-label={reveal ? "Hide password" : "Show password"}
                    className="absolute top-1/2 right-2 flex size-8 -translate-y-1/2 cursor-pointer items-center justify-center rounded-lg text-haze hover:text-frost"
                  >
                    {reveal ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                  </button>
                </div>
                {showHelp && (
                  <p id="password-help" className="pt-1 text-[13px] text-haze">
                    Passwords are reset by your campus administrator. Ask them for a one-time password, then sign in with it.
                  </p>
                )}
              </div>
              {error && (
                <p role="alert" className="rounded-xl bg-rose/10 px-3.5 py-2.5 text-sm text-rose ring-1 ring-rose/25 ring-inset">
                  {error}
                </p>
              )}
              <NxButton type="submit" variant="primary" size="lg" className="group w-full" disabled={submitting || !email || !password}>
                {submitting ? <Loader2 className="animate-spin" /> : null}
                Sign in
                {!submitting && <ArrowRight className="transition-transform group-hover:translate-x-0.5" />}
              </NxButton>
            </form>
            <p className="mt-6 flex items-center justify-center gap-1.5 text-xs text-dim">
              <LockKeyhole className="size-3.5" /> Your role decides what every agent may see and do.
            </p>
          </div>
        </section>
      </main>
    </div>
  );
}
