import { Loader2, LockKeyhole } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { ApiError } from "@/api/client";
import { homeRouteFor } from "@/auth/roles";
import { useAuth } from "@/auth/useAuth";
import { BrandMark } from "@/components/ui/brand";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { ErrorState, Notice } from "@/components/ui/states";

/** Optional institution name, configured per deployment (never hard-coded). */
const INSTITUTION = (import.meta.env.VITE_INSTITUTION_NAME as string | undefined)?.trim();

export function LoginPage() {
  const { status, user, login, notice } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
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
      const home = homeRouteFor(signedIn.role);
      navigate(from && from.startsWith(home) ? from : home, { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Sign-in failed. Please try again.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-dvh flex-col bg-background px-4 py-8 sm:py-12">
      <main className="flex flex-1 items-center justify-center">
        <div className="w-full max-w-[400px]">
          <div className="flex flex-col items-center text-center">
            <BrandMark className="size-11" />
            <p className="mt-3 text-base font-semibold tracking-tight text-ink">
              CampusNexus <span className="text-primary">AI</span>
            </p>
            {INSTITUTION && <p className="mt-0.5 text-sm text-muted">{INSTITUTION}</p>}
          </div>

          <div className="mt-8 rounded-card border border-border bg-surface p-6 shadow-[var(--shadow-raised)] sm:p-8">
            <h1 className="text-xl font-semibold tracking-tight text-ink">Welcome back</h1>
            <p className="mt-1 text-sm text-muted">Sign in to continue to CampusNexus</p>
            {notice && (
              <Notice tone="warning" className="mt-5">
                {notice}
              </Notice>
            )}
            <form className="mt-6 space-y-4" onSubmit={submit} noValidate>
              <div className="space-y-1.5">
                <Label htmlFor="email">Email</Label>
                <Input id="email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@campusnexus.local" />
              </div>
              <div className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <Label htmlFor="password">Password</Label>
                  <button
                    type="button"
                    onClick={() => setShowHelp((v) => !v)}
                    aria-expanded={showHelp}
                    aria-controls="password-help"
                    className="text-sm font-medium text-primary hover:underline"
                  >
                    Forgot password?
                  </button>
                </div>
                <Input id="password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
                {showHelp && (
                  <p id="password-help" className="pt-1 text-sm text-muted">
                    Passwords are reset by your campus administrator. Ask them for a one-time password, then sign in with it.
                  </p>
                )}
              </div>
              {error && <ErrorState message={error} />}
              <Button type="submit" className="w-full" size="lg" disabled={submitting || !email || !password}>
                {submitting && <Loader2 className="animate-spin" />}
                Sign in
              </Button>
            </form>
          </div>

          <p className="mt-6 flex items-center justify-center gap-1.5 text-xs text-muted">
            <LockKeyhole className="size-3.5" /> Secure role-based campus access
          </p>
        </div>
      </main>
      <p className="mt-8 text-center text-xs text-subtle">CampusNexus AI · Campus Intelligence Platform</p>
    </div>
  );
}
