import { ChevronsLeft, ChevronsRight, LogOut, Settings } from "lucide-react";
import { NavLink } from "react-router-dom";
import { navigationFor, settingsPathFor } from "@/components/layout/navigation";
import { useAuth } from "@/auth/useAuth";
import { cn } from "@/utils/cn";

function Logo({ collapsed }: { collapsed: boolean }) {
  return (
    <div className="flex h-14 items-center gap-2.5 px-4">
      <img src="/favicon.svg" alt="" className="size-7 shrink-0 rounded-md" />
      {!collapsed && (
        <div className="leading-tight">
          <div className="text-sm font-semibold text-white">CampusNexus</div>
          <div className="text-[11px] text-white/60">College Operating System</div>
        </div>
      )}
    </div>
  );
}

const itemClass = (active: boolean, collapsed: boolean) =>
  cn(
    "flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors [&_svg]:size-4 [&_svg]:shrink-0",
    collapsed && "justify-center px-0",
    active ? "bg-white/12 text-white font-medium" : "text-white/70 hover:bg-white/8 hover:text-white",
  );

export function Sidebar({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const { user, logout } = useAuth();
  if (!user) return null;
  const items = navigationFor(user.role);

  return (
    <aside
      className={cn("sticky top-0 flex h-screen shrink-0 flex-col bg-primary transition-[width] duration-200", collapsed ? "w-[68px]" : "w-60")}
      aria-label="Main navigation"
    >
      <Logo collapsed={collapsed} />
      <nav className="flex-1 space-y-0.5 overflow-y-auto px-3 py-3">
        {items.map((item) => (
          <NavLink key={item.to} to={item.to} end={item.end} title={collapsed ? item.label : undefined} className={({ isActive }) => itemClass(isActive, collapsed)}>
            <item.icon />
            {!collapsed && <span className="truncate">{item.label}</span>}
          </NavLink>
        ))}
      </nav>
      <div className="space-y-0.5 border-t border-white/10 px-3 py-3">
        <NavLink to={settingsPathFor(user.role)} title={collapsed ? "Settings" : undefined} className={({ isActive }) => itemClass(isActive, collapsed)}>
          <Settings />
          {!collapsed && <span>Settings</span>}
        </NavLink>
        <button type="button" onClick={() => void logout()} title={collapsed ? "Log out" : undefined} className={cn(itemClass(false, collapsed), "w-full")}>
          <LogOut />
          {!collapsed && <span>Log out</span>}
        </button>
        <button
          type="button"
          onClick={onToggle}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className={cn(itemClass(false, collapsed), "w-full text-white/50")}
        >
          {collapsed ? <ChevronsRight /> : <ChevronsLeft />}
          {!collapsed && <span>Collapse</span>}
        </button>
      </div>
    </aside>
  );
}
