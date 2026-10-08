import { Building2, LayoutGrid, LogOut, Mail, PanelRight, Settings, ShieldCheck, Volume2, X } from "lucide-react";
import { useId, useRef, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ROLE_LABELS, classicHomeFor } from "@/auth/roles";
import { useAuth } from "@/auth/useAuth";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { AiModeBadge } from "@/nexus/layout/ActivityRail";
import { NxButton } from "@/nexus/components/primitives";
import { setPreference, usePreferences } from "@/nexus/state/preferences";
import { cn } from "@/utils/cn";
import { initials } from "@/utils/format";

/** A right-hand frosted drawer for the dark surfaces (focus trapped; Escape and backdrop close it). */
export function NxSheet({ open, onClose, title, children, className }: { open: boolean; onClose: () => void; title: string; children: ReactNode; className?: string }) {
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useFocusTrap(panel, open, onClose);
  if (!open) return null;
  return (
    <div className="nx fixed inset-0 z-50 bg-void/60 backdrop-blur-sm animate-fade-in" onMouseDown={onClose}>
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
        className={cn("absolute inset-y-0 right-0 flex w-full max-w-[400px] flex-col border-l border-line bg-abyss/95 shadow-[-30px_0_80px_-20px_rgb(0_0_0/0.6)] backdrop-blur-2xl animate-sheet-right focus:outline-none", className)}
      >
        <div className="flex items-center justify-between border-b border-line px-5 py-4">
          <h2 id={titleId} className="font-display text-base font-semibold text-frost">
            {title}
          </h2>
          <NxButton variant="ghost" size="icon" onClick={onClose} aria-label="Close">
            <X />
          </NxButton>
        </div>
        <div className="nx-scroll flex-1 overflow-y-auto">{children}</div>
      </div>
    </div>
  );
}

function Toggle({ checked, onChange, label, hint, icon }: { checked: boolean; onChange: (v: boolean) => void; label: string; hint: string; icon: ReactNode }) {
  return (
    <label className="flex cursor-pointer items-start gap-3 rounded-xl px-3 py-3 transition-colors hover:bg-glass">
      <span className="mt-0.5 text-haze [&_svg]:size-4">{icon}</span>
      <span className="min-w-0 flex-1">
        <span className="block text-sm text-frost">{label}</span>
        <span className="block text-xs text-haze">{hint}</span>
      </span>
      <input type="checkbox" role="switch" className="peer sr-only" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="relative mt-0.5 h-5 w-9 shrink-0 rounded-full bg-raised ring-1 ring-line-strong transition-colors peer-checked:bg-gradient-to-r peer-checked:from-cyan peer-checked:to-electric peer-focus-visible:outline-2 peer-focus-visible:outline-cyan after:absolute after:top-0.5 after:left-0.5 after:size-4 after:rounded-full after:bg-white after:transition-transform peer-checked:after:translate-x-4" aria-hidden />
    </label>
  );
}

/** Profile, preferences and sign-out. */
export function ProfilePanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { user, logout } = useAuth();
  const prefs = usePreferences();
  if (!user) return null;
  const classic = classicHomeFor(user.role);
  return (
    <NxSheet open={open} onClose={onClose} title="Profile & settings">
      <div className="px-5 pt-6 pb-5">
        <div className="flex items-center gap-4">
          <span className="flex size-14 items-center justify-center rounded-2xl bg-gradient-to-br from-cyan/30 to-violet/40 font-display text-lg font-bold text-frost ring-1 ring-line-strong">
            {initials(user.display_name)}
          </span>
          <div className="min-w-0">
            <p className="truncate font-display text-lg font-semibold text-frost">{user.display_name}</p>
            <p className="text-sm text-haze">{ROLE_LABELS[user.role]}</p>
          </div>
        </div>
        <dl className="mt-5 space-y-2.5 text-sm">
          <div className="flex items-center gap-2.5 text-mist">
            <Mail className="size-4 text-dim" />
            <dt className="sr-only">Email</dt>
            <dd className="truncate">{user.email}</dd>
          </div>
          {(user.department_name || user.organization) && (
            <div className="flex items-center gap-2.5 text-mist">
              <Building2 className="size-4 text-dim" />
              <dt className="sr-only">Department</dt>
              <dd className="truncate">{[user.department_name, user.organization?.name].filter(Boolean).join(" · ")}</dd>
            </div>
          )}
          {user.student_id && (
            <div className="flex items-center gap-2.5 text-mist">
              <ShieldCheck className="size-4 text-dim" />
              <dt className="sr-only">Student ID</dt>
              <dd>{user.student_id}</dd>
            </div>
          )}
        </dl>
      </div>

      <div className="border-t border-line px-2 py-3">
        <p className="px-3 pb-1 text-[11px] font-semibold tracking-[0.14em] text-haze uppercase">Preferences</p>
        <Toggle icon={<Volume2 />} label="Spoken replies" hint="Nexus answers out loud during voice sessions." checked={prefs.speakReplies} onChange={(v) => setPreference("speakReplies", v)} />
        <Toggle icon={<PanelRight />} label="Live activity rail" hint="Show missions and alerts beside the assistant on wide screens." checked={prefs.showActivityRail} onChange={(v) => setPreference("showActivityRail", v)} />
      </div>

      <div className="border-t border-line px-5 py-4">
        <div className="flex items-center justify-between">
          <span className="text-sm text-mist">AI mode</span>
          <AiModeBadge />
        </div>
        <p className="mt-2 text-xs leading-relaxed text-haze">
          Nexus only reads and plans. Anything that changes the world goes through deterministic checks and, when sensitive, your approval — every step is audited.
        </p>
      </div>

      <div className="space-y-1 border-t border-line px-2 py-3">
        {classic && (
          <Link to={classic} onClick={onClose} className="flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm text-mist transition-colors hover:bg-glass hover:text-frost">
            <LayoutGrid className="size-4 text-haze" /> Classic workspace
          </Link>
        )}
        {classic && (
          <Link to={`${classic}/settings`} onClick={onClose} className="flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm text-mist transition-colors hover:bg-glass hover:text-frost">
            <Settings className="size-4 text-haze" /> Account settings
          </Link>
        )}
        <button type="button" onClick={() => void logout()} className="flex w-full cursor-pointer items-center gap-3 rounded-xl px-3 py-2.5 text-sm text-rose transition-colors hover:bg-rose/10">
          <LogOut className="size-4" /> Sign out
        </button>
      </div>
    </NxSheet>
  );
}
