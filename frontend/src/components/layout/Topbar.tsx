import { Bell, LogOut, Menu, Search, Settings, Sparkles } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { enquiryPathFor, sectionTitleFor, settingsPathFor } from "@/components/layout/navigation";
import { Avatar } from "@/components/ui/avatar";
import { BrandMark } from "@/components/ui/brand";
import { Button } from "@/components/ui/button";
import { buttonVariants } from "@/components/ui/button-variants";
import { Popover } from "@/components/ui/popover";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { ROLE_LABELS } from "@/auth/roles";
import { useAuth } from "@/auth/useAuth";
import { useAdminNotifications } from "@/hooks/useAdminData";
import { useStaffNotifications } from "@/hooks/useHodData";
import { useNotifications } from "@/hooks/useStudentData";
import type { Role } from "@/types/api";
import { relativeTime, titleCase } from "@/utils/format";

/** Global "Ask CampusNexus": hands the question to the role's own read-only Enquiry Agent. */
function AskCampusNexus({ role }: { role: Role }) {
  const [value, setValue] = useState("");
  const navigate = useNavigate();
  const target = enquiryPathFor(role);
  if (!target) return null;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const question = value.trim();
    if (!question) return;
    setValue("");
    navigate(`${target}?q=${encodeURIComponent(question)}`);
  };
  return (
    <>
      <form onSubmit={submit} className="relative hidden w-full max-w-sm md:block lg:max-w-md" role="search">
        <Sparkles className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-primary" />
        <input
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="Ask CampusNexus anything…"
          aria-label="Ask CampusNexus"
          className="h-9 w-full rounded-md border border-border bg-surface-muted pr-3 pl-9 text-sm transition-colors placeholder:text-subtle hover:border-border-strong focus:border-primary focus:bg-surface focus:ring-3 focus:ring-primary/15 focus:outline-none"
        />
      </form>
      <Link to={target} aria-label="Ask CampusNexus" className={buttonVariants({ variant: "ghost", size: "icon", className: "md:hidden" })}>
        <Search />
      </Link>
    </>
  );
}

function NotificationsMenu({ feed }: { feed: "student" | "staff" | "admin" }) {
  const student = useNotifications(feed === "student");
  const staffFeed = useStaffNotifications(feed === "staff");
  const adminFeed = useAdminNotifications(feed === "admin");
  const { data, isLoading, isError, error, refetch } = feed === "admin" ? adminFeed : feed === "staff" ? staffFeed : student;
  const unread = data?.filter((n) => n.status !== "read").length ?? 0;
  return (
    <Popover
      className="sm:w-96"
      trigger={({ toggle, open }) => (
        <Button variant="ghost" size="icon" aria-label={`Notifications${unread ? ` (${unread} unread)` : ""}`} aria-expanded={open} onClick={toggle} className="relative">
          <Bell />
          {unread > 0 && <span className="absolute top-2 right-2 size-2 rounded-full bg-danger ring-2 ring-surface" />}
        </Button>
      )}
    >
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <span className="text-sm font-semibold">Notifications</span>
        {unread > 0 && <span className="text-xs text-muted">{unread} unread</span>}
      </div>
      <div className="max-h-[min(24rem,70dvh)] overflow-y-auto p-1.5">
        {isLoading && <SkeletonRows rows={3} className="p-2" />}
        {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
        {data && data.length === 0 && <EmptyState compact title="You're all caught up." description="New notifications appear here." />}
        {data?.map((n) => (
          <div key={n.id} className="rounded-md px-3 py-2.5 hover:bg-surface-muted">
            <div className="flex items-baseline justify-between gap-3">
              <span className="min-w-0 truncate text-sm font-medium">{n.title}</span>
              <span className="shrink-0 text-xs text-subtle">{relativeTime(n.created_at)}</span>
            </div>
            <p className="mt-0.5 line-clamp-2 text-[13px] text-muted">{n.body}</p>
            <span className="mt-1 inline-block text-xs text-subtle">{titleCase(n.category)}</span>
          </div>
        ))}
      </div>
    </Popover>
  );
}

function UserMenu() {
  const { user, logout } = useAuth();
  if (!user) return null;
  return (
    <Popover
      className="sm:w-60"
      trigger={({ toggle, open }) => (
        <button type="button" onClick={toggle} aria-expanded={open} aria-label={`Account: ${user.display_name}`} className="rounded-full p-0.5 transition-colors hover:bg-surface-muted">
          <Avatar name={user.display_name} />
        </button>
      )}
    >
      {({ close }) => (
        <div className="p-1.5">
          <div className="px-3 py-2">
            <p className="truncate text-sm font-medium">{user.display_name}</p>
            <p className="truncate text-xs text-muted">{user.email}</p>
            <p className="mt-0.5 text-xs text-muted">
              {ROLE_LABELS[user.role]}
              {user.department_code ? ` · ${user.department_code}` : ""}
            </p>
          </div>
          <div className="my-1 h-px bg-border" />
          <Link to={settingsPathFor(user.role)} onClick={close} className="flex items-center gap-2.5 rounded-md px-3 py-2 text-sm hover:bg-surface-muted [&_svg]:size-4 [&_svg]:text-muted">
            <Settings /> Settings
          </Link>
          <button type="button" onClick={() => void logout()} className="flex w-full items-center gap-2.5 rounded-md px-3 py-2 text-sm hover:bg-surface-muted [&_svg]:size-4 [&_svg]:text-muted">
            <LogOut /> Sign out
          </button>
        </div>
      )}
    </Popover>
  );
}

export function Topbar({ onOpenNav }: { onOpenNav: () => void }) {
  const { user } = useAuth();
  const { pathname } = useLocation();
  if (!user) return null;
  const feed = user.role === "student" ? "student" : user.role === "admin" ? "admin" : "staff";
  return (
    <header className="sticky top-0 z-30 border-b border-border bg-surface">
      <div className="flex h-14 items-center gap-2 px-3 sm:gap-4 sm:px-6 lg:px-8">
        <Button variant="ghost" size="icon" aria-label="Open navigation" onClick={onOpenNav} className="md:hidden">
          <Menu />
        </Button>
        <BrandMark className="size-7 md:hidden" />
        <p className="min-w-0 shrink-0 truncate text-[15px] font-semibold text-ink" aria-current="page">
          {sectionTitleFor(user.role, pathname)}
        </p>
        <div className="flex min-w-0 flex-1 items-center justify-end">
          <AskCampusNexus role={user.role} />
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {user.role !== "staff" && <NotificationsMenu feed={feed} />}
          <UserMenu />
        </div>
      </div>
    </header>
  );
}
