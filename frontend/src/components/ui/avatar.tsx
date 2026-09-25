import { initials } from "@/utils/format";
import { cn } from "@/utils/cn";

export function Avatar({ name, className }: { name: string; className?: string }) {
  return (
    <span aria-hidden className={cn("flex size-8 shrink-0 items-center justify-center rounded-full bg-primary-soft text-xs font-semibold text-primary", className)}>
      {initials(name)}
    </span>
  );
}
