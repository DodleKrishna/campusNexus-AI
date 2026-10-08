import { PanelRight } from "lucide-react";
import { Suspense, useMemo, useState } from "react";
import { Link, Outlet } from "react-router-dom";
import { useAuth } from "@/auth/useAuth";
import { CampusLogo } from "@/nexus/components/brand";
import { GlowSkeleton, NxButton } from "@/nexus/components/primitives";
import { VoiceOverlay } from "@/nexus/components/VoiceOverlay";
import { ActivityRail } from "@/nexus/layout/ActivityRail";
import { AmbientBackground } from "@/nexus/layout/AmbientBackground";
import { MobileTabBar, NavRail } from "@/nexus/layout/NavRail";
import { NxSheet, ProfilePanel } from "@/nexus/layout/ProfilePanel";
import { ShellContext, type ShellContextValue } from "@/nexus/layout/ShellContext";
import { AssistantProvider } from "@/nexus/state/AssistantProvider";
import { usePreferences } from "@/nexus/state/preferences";
import { initials } from "@/utils/format";

function PageFallback() {
  return (
    <div className="mx-auto w-full max-w-3xl space-y-4 px-6 py-16">
      <GlowSkeleton className="h-6 w-1/3" />
      <GlowSkeleton className="w-2/3" />
      <GlowSkeleton className="w-1/2" />
    </div>
  );
}

/**
 * The CAMPUS AI layout: a slim navigation rail, the assistant workspace in the
 * centre and the live activity/context rail on the right. The conversation and
 * the voice session belong to the shell, so they survive moving between pages.
 */
export function NexusShell() {
  const { user } = useAuth();
  const prefs = usePreferences();
  const [voiceOpen, setVoiceOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);

  const shell = useMemo<ShellContextValue>(
    () => ({ openVoice: () => setVoiceOpen(true), openProfile: () => setProfileOpen(true), openActivity: () => setActivityOpen(true) }),
    [],
  );
  if (!user) return null;

  return (
    <ShellContext.Provider value={shell}>
      <AssistantProvider>
        <div className="nx relative flex h-dvh overflow-hidden">
          <a href="#nexus-main" className="sr-only z-50 rounded-lg bg-raised px-3 py-2 text-sm text-frost focus:not-sr-only focus:fixed focus:top-3 focus:left-3">
            Skip to content
          </a>
          <AmbientBackground />
          <NavRail />

          <div className="relative flex min-w-0 flex-1 flex-col">
            {/* Mobile / tablet top bar. */}
            <header className="relative z-20 flex items-center justify-between border-b border-line bg-abyss/60 px-4 py-3 backdrop-blur-xl md:hidden">
              <Link to="/nexus" aria-label="CAMPUS AI home">
                <CampusLogo />
              </Link>
              <div className="flex items-center gap-1">
                <NxButton variant="ghost" size="icon" onClick={shell.openActivity} aria-label="Live activity">
                  <PanelRight />
                </NxButton>
                <button
                  type="button"
                  onClick={shell.openProfile}
                  aria-label={`Account: ${user.display_name}`}
                  className="flex size-9 cursor-pointer items-center justify-center rounded-full bg-gradient-to-br from-cyan/30 to-violet/40 text-xs font-semibold text-frost ring-1 ring-line-strong"
                >
                  {initials(user.display_name)}
                </button>
              </div>
            </header>
            {/* Tablet+: open the activity rail when it is not docked. */}
            <NxButton
              variant="glass"
              size="icon"
              onClick={shell.openActivity}
              aria-label="Live activity"
              className={prefs.showActivityRail ? "absolute top-5 right-5 z-20 hidden md:inline-flex xl:hidden" : "absolute top-5 right-5 z-20 hidden md:inline-flex"}
            >
              <PanelRight />
            </NxButton>

            <main id="nexus-main" tabIndex={-1} className="nx-scroll relative z-10 flex min-h-0 flex-1 flex-col overflow-y-auto pb-20 focus:outline-none md:pb-0">
              <Suspense fallback={<PageFallback />}>
                <Outlet />
              </Suspense>
            </main>
          </div>

          {prefs.showActivityRail && <ActivityRail className="relative z-10 hidden w-[340px] shrink-0 border-l border-line bg-abyss/40 backdrop-blur-xl xl:flex" />}

          <MobileTabBar />
        </div>

        <NxSheet open={activityOpen} onClose={() => setActivityOpen(false)} title="Live activity">
          <ActivityRail />
        </NxSheet>
        <ProfilePanel open={profileOpen} onClose={() => setProfileOpen(false)} />
        {voiceOpen && <VoiceOverlay onClose={() => setVoiceOpen(false)} />}
      </AssistantProvider>
    </ShellContext.Provider>
  );
}
