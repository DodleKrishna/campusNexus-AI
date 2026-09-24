import { Bell, Search } from "lucide-react";
import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ModeBadge } from "@/components/layout/ModeBadge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Popover } from "@/components/ui/popover";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { ROLE_LABELS } from "@/auth/roles";
import { useAuth } from "@/auth/useAuth";
import { useStaffNotifications } from "@/hooks/useHodData";
import { useNotifications } from "@/hooks/useStudentData";
import { initials, relativeTime, titleCase } from "@/utils/format";

function AskCampusNexus() {
  const [value, setValue] = useState("");
  const navigate = useNavigate();
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const question = value.trim();
    if (!question) return;
    setValue("");
    navigate(`/student/agents/enquiry?q=${encodeURIComponent(question)}`);
  };
  return (
    <form onSubmit={submit} className="relative w-full max-w-md" role="search">
      <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-subtle" />
      <input
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder="Ask CampusNexus…  e.g. Do I have anything important today?"
        aria-label="Ask CampusNexus"
        className="h-9 w-full rounded-lg border border-border bg-surface-muted pl-9 pr-3 text-sm placeholder:text-subtle focus:border-accent focus:bg-surface focus:outline-none focus:ring-2 focus:ring-accent/20"
      />
    </form>
  );
}

function NotificationsMenu({ staff = false }: { staff?: boolean }) {
  const student = useNotifications(!staff);
  const staffFeed = useStaffNotifications(staff);
  const { data, isLoading, isError, error, refetch } = staff ? staffFeed : student;
  const unread = data?.filter((n) => n.status !== "read").length ?? 0;
  return (
    <Popover
      className="w-80"
      trigger={({ toggle }) => (
        <Button variant="ghost" size="icon" aria-label={`Notifications${unread ? ` (${unread} unread)` : ""}`} onClick={toggle} className="relative">
          <Bell />
          {unread > 0 && <span className="absolute right-1.5 top-1.5 size-2 rounded-full bg-accent" />}
        </Button>
      )}
    >
      <div className="border-b border-border px-4 py-3 text-sm font-semibold">Notifications</div>
      <div className="max-h-96 overflow-y-auto p-2">
        {isLoading && <div className="p-2"><SkeletonRows rows={3} /></div>}
        {isError && <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />}
        {data && data.length === 0 && <EmptyState title="No notifications" className="m-2" />}
        {data?.map((n) => (
          <div key={n.id} className="rounded-lg px-3 py-2.5 hover:bg-surface-muted">
            <div className="flex items-center justify-between gap-2">
              <span className="truncate text-sm font-medium">{n.title}</span>
              <span className="shrink-0 text-[11px] text-subtle">{relativeTime(n.created_at)}</span>
            </div>
            <p className="mt-0.5 line-clamp-2 text-xs text-muted">{n.body}</p>
            <Badge className="mt-1.5" tone="neutral">{titleCase(n.category)}</Badge>
          </div>
        ))}
      </div>
    </Popover>
  );
}

export function Topbar() {
  const { user } = useAuth();
  if (!user) return null;
  const isStudent = user.role === "student";
  return (
    <header className="sticky top-0 z-30 flex h-14 items-center gap-4 border-b border-border bg-surface/95 px-6 backdrop-blur">
      <div className="flex flex-1 items-center">{isStudent && <AskCampusNexus />}</div>
      <ModeBadge />
      {isStudent && <NotificationsMenu />}
      {(user.role === "faculty" || user.role === "hod") && <NotificationsMenu staff />}
      <div className="flex items-center gap-2.5 border-l border-border pl-4">
        <div className="flex size-8 items-center justify-center rounded-full bg-primary-soft text-xs font-semibold text-primary" aria-hidden>
          {initials(user.display_name)}
        </div>
        <div className="hidden leading-tight sm:block">
          <div className="text-sm font-medium">{user.display_name}</div>
          <div className="text-[11px] text-muted">{ROLE_LABELS[user.role]}</div>
        </div>
      </div>
    </header>
  );
}
