import { Search } from "lucide-react";
import { forwardRef, type InputHTMLAttributes, type LabelHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";
import { cn } from "@/utils/cn";

const field =
  "w-full rounded-md border border-border bg-surface px-3 text-sm text-ink placeholder:text-subtle transition-colors duration-150 hover:border-border-strong focus:border-primary focus:outline-none focus:ring-3 focus:ring-primary/15 disabled:opacity-60";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(({ className, ...props }, ref) => (
  <input ref={ref} className={cn(field, "h-10", className)} {...props} />
));
Input.displayName = "Input";

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(({ className, ...props }, ref) => (
  <textarea ref={ref} className={cn(field, "py-2.5 leading-relaxed", className)} {...props} />
));
Textarea.displayName = "Textarea";

export function Select({ className, children, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={cn(field, "h-9 w-auto max-w-full cursor-pointer pr-8", className)} {...props}>
      {children}
    </select>
  );
}

/** A search field with a leading icon; always pass an aria-label or a <Label>. */
export function SearchInput({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <div className={cn("relative w-full sm:max-w-xs", className)}>
      <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-subtle" />
      <input type="search" className={cn(field, "h-9 pl-9")} {...props} />
    </div>
  );
}

export function Label({ className, ...props }: LabelHTMLAttributes<HTMLLabelElement>) {
  return <label className={cn("text-sm font-medium text-ink", className)} {...props} />;
}

/** A row of filters/search above a list or table; wraps on small screens. */
export function Toolbar({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-center", className)}>{children}</div>;
}
