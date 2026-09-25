import { LogOut, Settings, X } from "lucide-react";
import { NavLink } from "react-router-dom";
import { navigationFor, settingsPathFor, WORKSPACE_LABELS } from "@/components/layout/navigation";
import { Avatar } from "@/components/ui/avatar";
import { BrandMark, Logo } from "@/components/ui/brand";
import { Sheet } from "@/components/ui/sheet";
import { ROLE_LABELS } from "@/auth/roles";
import { useAuth } from "@/auth/useAuth";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { cn } from "@/utils/cn";

const itemClass = (active: boolean, rail: boolean) =>
  cn(
    "group relative flex h-9 items-center gap-2.5 rounded-md px-2.5 text-[13px] transition-colors duration-150 [&_svg]:size-4 [&_svg]:shrink-0",
    rail && "justify-center px-0",
    active ? "bg-nav-raised font-medium text-white [&_svg]:text-accent-bright" : "text-nav-text hover:bg-nav-raised/60 hover:text-white",
  );

/** Navigation shared by the desktop sidebar, the tablet icon rail and the mobile drawer. */
function NavContent({ rail, onNavigate }: { rail: boolean; onNavigate?: () => void }) {
  const { user, logout } = useAuth();
  if (!user) return null;
  const items = navigationFor(user.role);
  return (
    <>
      {!rail && <p className="px-5 pt-1 pb-2 text-[11px] font-semibold tracking-wider text-nav-text/70 uppercase">{WORKSPACE_LABELS[user.role]}</p>}
      <nav aria-label="Main navigation" className={cn("flex-1 space-y-0.5 overflow-y-auto pb-3", rail ? "px-2 pt-2" : "px-3")}>
        {items.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            aria-label={item.label}
            title={rail ? item.label : undefined}
            onClick={onNavigate}
            className={({ isActive }) => itemClass(isActive, rail)}
          >
            {({ isActive }) => (
              <>
                {isActive && <span aria-hidden className="absolute top-1.5 bottom-1.5 left-0 w-0.5 rounded-full bg-primary" />}
                <item.icon />
                {!rail && <span className="truncate">{item.label}</span>}
              </>
            )}
          </NavLink>
        ))}
      </nav>
      <div className={cn("space-y-0.5 border-t border-nav-border py-3", rail ? "px-2" : "px-3")}>
        <NavLink to={settingsPathFor(user.role)} aria-label="Settings" title={rail ? "Settings" : undefined} onClick={onNavigate} className={({ isActive }) => itemClass(isActive, rail)}>
          <Settings />
          {!rail && <span>Settings</span>}
        </NavLink>
        <button type="button" onClick={() => void logout()} aria-label="Sign out" title={rail ? "Sign out" : undefined} className={cn(itemClass(false, rail), "w-full")}>
          <LogOut />
          {!rail && <span>Sign out</span>}
        </button>
      </div>
      <div className={cn("flex items-center gap-2.5 border-t border-nav-border py-3", rail ? "justify-center px-2" : "px-4")} title={rail ? user.display_name : undefined}>
        <Avatar name={user.display_name} className="bg-nav-raised text-white" />
        {!rail && (
          <div className="min-w-0 leading-tight">
            <p className="truncate text-[13px] font-medium text-white">{user.display_name}</p>
            <p className="truncate text-xs text-nav-text">{ROLE_LABELS[user.role]}</p>
          </div>
        )}
      </div>
    </>
  );
}

/** Desktop (>= 1024px): full 232px sidebar. Tablet (768-1023px): icon rail. Mobile: hidden (see MobileNav). */
export function Sidebar() {
  const { user } = useAuth();
  const desktop = useMediaQuery("(min-width: 1024px)");
  if (!user) return null;
  const rail = !desktop;
  return (
    <aside className={cn("sticky top-0 hidden h-dvh shrink-0 flex-col bg-nav md:flex", rail ? "w-16" : "w-[232px]")}>
      <div className={cn("flex h-14 shrink-0 items-center", rail ? "justify-center" : "px-5")}>{rail ? <BrandMark className="size-7" /> : <Logo tone="light" markClassName="size-7" />}</div>
      <NavContent rail={rail} />
    </aside>
  );
}

/** Mobile (< 768px): the same navigation in a left drawer. */
export function MobileNav({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { user } = useAuth();
  if (!user) return null;
  return (
    <Sheet open={open} onClose={onClose} title="Navigation" side="left" hideHeader className="bg-nav">
      <div className="flex h-full flex-col">
        <div className="flex h-14 shrink-0 items-center justify-between gap-2 pr-3 pl-5">
          <Logo tone="light" markClassName="size-7" />
          <button type="button" onClick={onClose} aria-label="Close navigation" className="flex size-9 items-center justify-center rounded-md text-nav-text hover:bg-nav-raised hover:text-white">
            <X className="size-5" />
          </button>
        </div>
        <NavContent rail={false} onNavigate={onClose} />
      </div>
    </Sheet>
  );
}
