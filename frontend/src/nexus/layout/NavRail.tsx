import { Bell, Bot, LayoutGrid, Settings2, Sparkles, Target, type LucideIcon } from "lucide-react";
import { NavLink, useLocation } from "react-router-dom";
import { classicHomeFor } from "@/auth/roles";
import { useAuth } from "@/auth/useAuth";
import { CampusMark } from "@/nexus/components/brand";
import { useAlerts } from "@/nexus/hooks/useNexusData";
import { useShell } from "@/nexus/layout/ShellContext";
import { cn } from "@/utils/cn";
import { initials } from "@/utils/format";

interface RailItem {
  label: string;
  to: string;
  icon: LucideIcon;
  end?: boolean;
}

const RAIL_ITEMS: RailItem[] = [
  { label: "Nexus", to: "/nexus", icon: Sparkles, end: true },
  { label: "Missions", to: "/nexus/missions", icon: Target },
  { label: "Agents", to: "/nexus/agents", icon: Bot },
  { label: "Alerts", to: "/nexus/notifications", icon: Bell },
];

function useUnread(): number {
  const { user } = useAuth();
  const alerts = useAlerts(user!.role);
  return alerts.data?.filter((n) => n.status !== "read").length ?? 0;
}

function RailLink({ item, unread }: { item: RailItem; unread: number }) {
  const Icon = item.icon;
  return (
    <NavLink
      to={item.to}
      end={item.end}
      aria-label={item.label}
      className={({ isActive }) =>
        cn(
          "group relative flex w-full flex-col items-center gap-1 rounded-2xl py-2.5 text-[10.5px] font-medium tracking-wide transition-colors",
          isActive ? "text-frost" : "text-haze hover:text-mist",
        )
      }
    >
      {({ isActive }) => (
        <>
          {isActive && <span className="absolute top-1/2 -left-3 h-6 w-[3px] -translate-y-1/2 rounded-r-full bg-gradient-to-b from-cyan to-violet shadow-[0_0_12px_rgb(34_211_238/0.8)]" />}
          <span
            className={cn(
              "relative flex size-10 items-center justify-center rounded-xl transition-all duration-300",
              isActive ? "bg-gradient-to-br from-cyan/20 to-violet/20 ring-1 ring-cyan/30 shadow-[0_0_24px_-6px_rgb(34_211_238/0.6)]" : "group-hover:bg-glass-strong",
            )}
          >
            <Icon className="size-[19px]" />
            {item.label === "Alerts" && unread > 0 && <span className="absolute top-1.5 right-1.5 size-2 rounded-full bg-rose ring-2 ring-abyss" />}
          </span>
          {item.label}
        </>
      )}
    </NavLink>
  );
}

/** Desktop: a slim vertical rail. Mobile: a bottom tab bar. */
export function NavRail() {
  const { user } = useAuth();
  const { openProfile } = useShell();
  const unread = useUnread();
  const classic = classicHomeFor(user!.role);
  return (
    <nav aria-label="Primary" className="relative z-20 hidden w-[84px] shrink-0 flex-col items-center border-r border-line bg-abyss/60 px-3 py-5 backdrop-blur-xl md:flex">
      <NavLink to="/nexus" aria-label="CAMPUS AI home" className="mb-7">
        <CampusMark className="size-10" animated />
      </NavLink>
      <div className="flex w-full flex-1 flex-col items-center gap-1.5">
        {RAIL_ITEMS.map((item) => (
          <RailLink key={item.to} item={item} unread={unread} />
        ))}
      </div>
      <div className="flex w-full flex-col items-center gap-1.5">
        {classic && (
          <NavLink to={classic} title="Classic workspace" aria-label="Classic workspace" className="flex size-10 items-center justify-center rounded-xl text-haze transition-colors hover:bg-glass-strong hover:text-mist">
            <LayoutGrid className="size-[18px]" />
          </NavLink>
        )}
        <button type="button" onClick={openProfile} title="Settings" aria-label="Settings" className="flex size-10 cursor-pointer items-center justify-center rounded-xl text-haze transition-colors hover:bg-glass-strong hover:text-mist">
          <Settings2 className="size-[18px]" />
        </button>
        <button
          type="button"
          onClick={openProfile}
          aria-label={`Account: ${user!.display_name}`}
          className="mt-2 flex size-10 cursor-pointer items-center justify-center rounded-full bg-gradient-to-br from-cyan/30 to-violet/40 text-xs font-semibold text-frost ring-1 ring-line-strong transition-transform hover:scale-105"
        >
          {initials(user!.display_name)}
        </button>
      </div>
    </nav>
  );
}

export function MobileTabBar() {
  const unread = useUnread();
  const { pathname } = useLocation();
  return (
    <nav aria-label="Primary" className="fixed inset-x-0 bottom-0 z-30 border-t border-line bg-abyss/85 px-2 pt-1.5 pb-[max(env(safe-area-inset-bottom),0.5rem)] backdrop-blur-xl md:hidden">
      <div className="grid grid-cols-4">
        {RAIL_ITEMS.map((item) => {
          const Icon = item.icon;
          const active = item.end ? pathname === item.to : pathname.startsWith(item.to);
          return (
            <NavLink key={item.to} to={item.to} end={item.end} className={cn("relative flex flex-col items-center gap-0.5 py-1.5 text-[10.5px] font-medium", active ? "text-cyan" : "text-haze")}>
              <Icon className="size-5" />
              {item.label}
              {item.label === "Alerts" && unread > 0 && <span className="absolute top-1 right-[calc(50%-14px)] size-2 rounded-full bg-rose" />}
            </NavLink>
          );
        })}
      </div>
    </nav>
  );
}
