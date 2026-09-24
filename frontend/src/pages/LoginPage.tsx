import { Loader2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { ApiError } from "@/api/client";
import { homeRouteFor } from "@/auth/roles";
import { useAuth } from "@/auth/useAuth";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { ErrorState, Notice } from "@/components/ui/states";

const HIGHLIGHTS = [
  ["Academics", "Attendance, eligibility and exams, computed from your records."],
  ["Opportunities", "Events and internships matched to your schedule and skills."],
  ["Campus services", "Complaints tracked against their service deadlines."],
  ["Approvals", "Agents prepare actions; people with authority approve them."],
];

export function LoginPage() {
  const { status, user, login, notice } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

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
    <div className="grid min-h-screen lg:grid-cols-[1.1fr_1fr]">
      <section className="hidden flex-col justify-between bg-primary px-14 py-12 text-white lg:flex">
        <div className="flex items-center gap-3">
          <img src="/favicon.svg" alt="" className="size-9 rounded-lg" />
          <div>
            <div className="font-semibold">CampusNexus AI</div>
            <div className="text-xs text-white/60">College Operating System</div>
          </div>
        </div>
        <div className="max-w-lg">
          <h1 className="text-4xl font-semibold leading-tight tracking-tight">Your intelligent campus workspace</h1>
          <p className="mt-4 text-base leading-relaxed text-white/75">
            Academics, opportunities, campus services and approvals — coordinated by specialized AI agents.
          </p>
          <dl className="mt-10 grid grid-cols-2 gap-x-8 gap-y-6">
            {HIGHLIGHTS.map(([title, text]) => (
              <div key={title}>
                <dt className="text-sm font-semibold text-accent-soft">{title}</dt>
                <dd className="mt-1 text-sm leading-relaxed text-white/65">{text}</dd>
              </div>
            ))}
          </dl>
        </div>
        <p className="text-xs text-white/45">Meridian University · local development environment</p>
      </section>

      <section className="flex items-center justify-center px-6 py-12">
        <Card className="w-full max-w-sm px-7 py-8">
          <h2 className="text-xl font-semibold">Sign in</h2>
          <p className="mt-1 text-sm text-muted">Use your CampusNexus account.</p>
          {notice && <Notice tone="warning" className="mt-5">{notice}</Notice>}
          <form className="mt-6 space-y-4" onSubmit={submit} noValidate>
            <div className="space-y-1.5">
              <Label htmlFor="email">Email</Label>
              <Input id="email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@campusnexus.local" />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="password">Password</Label>
              <Input id="password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
            </div>
            {error && <ErrorState message={error} />}
            <Button type="submit" className="w-full" size="lg" disabled={submitting || !email || !password}>
              {submitting && <Loader2 className="animate-spin" />}
              Sign in
            </Button>
          </form>
        </Card>
      </section>
    </div>
  );
}
