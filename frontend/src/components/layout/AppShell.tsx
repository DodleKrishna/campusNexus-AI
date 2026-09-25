import { Suspense, useState } from "react";
import { Outlet } from "react-router-dom";
import { MobileNav, Sidebar } from "@/components/layout/Sidebar";
import { Topbar } from "@/components/layout/Topbar";
import { PageSkeleton } from "@/components/ui/skeleton";

/** The authenticated layout: dark sidebar (or rail / drawer), compact top bar, white workspace. */
export function AppShell() {
  const [navOpen, setNavOpen] = useState(false);
  return (
    <div className="flex min-h-dvh">
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-surface px-3 py-2 text-sm font-medium shadow-[var(--shadow-popover)] focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
      >
        Skip to content
      </a>
      <Sidebar />
      <MobileNav open={navOpen} onClose={() => setNavOpen(false)} />
      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar onOpenNav={() => setNavOpen(true)} />
        <main id="main" tabIndex={-1} className="mx-auto flex w-full max-w-[1360px] flex-1 flex-col px-4 py-6 [--sticky-offset:3.5rem] focus:outline-none sm:px-6 lg:px-8">
          <Suspense fallback={<PageSkeleton />}>
            <Outlet />
          </Suspense>
        </main>
      </div>
    </div>
  );
}
