import { Suspense, useState } from "react";
import { Outlet } from "react-router-dom";
import { Sidebar } from "@/components/layout/Sidebar";
import { Topbar } from "@/components/layout/Topbar";
import { FullPageLoader } from "@/components/ui/full-page-loader";

const COLLAPSE_KEY = "campusnexus.sidebar.collapsed";

function initialCollapsed(): boolean {
  try {
    return window.localStorage.getItem(COLLAPSE_KEY) === "1";
  } catch {
    return false;
  }
}

export function AppShell() {
  const [collapsed, setCollapsed] = useState(initialCollapsed);
  const toggle = () =>
    setCollapsed((value) => {
      try {
        window.localStorage.setItem(COLLAPSE_KEY, value ? "0" : "1");
      } catch {
        /* ignore */
      }
      return !value;
    });

  return (
    <div className="flex min-h-screen">
      <Sidebar collapsed={collapsed} onToggle={toggle} />
      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar />
        <main className="mx-auto w-full max-w-[1320px] flex-1 px-6 py-6">
          <Suspense fallback={<FullPageLoader label="Loading workspace…" />}>
            <Outlet />
          </Suspense>
        </main>
      </div>
    </div>
  );
}
