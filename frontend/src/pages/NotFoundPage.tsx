import { Compass } from "lucide-react";
import { Link } from "react-router-dom";
import { BrandMark } from "@/components/ui/brand";
import { buttonVariants } from "@/components/ui/button-variants";

export function NotFoundPage() {
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center bg-background px-6 text-center">
      <BrandMark className="size-10" />
      <div className="mt-8 flex size-12 items-center justify-center rounded-full bg-surface-muted text-subtle">
        <Compass className="size-6" />
      </div>
      <p className="mt-4 text-sm font-medium text-accent-hover">404</p>
      <h1 className="mt-1 text-xl font-semibold text-ink">This page doesn't exist</h1>
      <p className="mt-1 max-w-sm text-sm text-muted">The link may be out of date, or the page was moved.</p>
      <Link to="/" className={buttonVariants({ variant: "primary", className: "mt-6" })}>
        Go to your workspace
      </Link>
    </div>
  );
}
