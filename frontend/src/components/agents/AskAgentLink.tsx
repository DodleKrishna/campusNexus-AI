import { Sparkles } from "lucide-react";
import { Link } from "react-router-dom";
import { buttonVariants } from "@/components/ui/button-variants";

/**
 * Contextual entry into an agent from inside a module ("Ask Academic Agent" on the
 * attendance page). An optional question is handed over through ?q= and sent once.
 */
export function AskAgentLink({ to, label, question }: { to: string; label: string; question?: string }) {
  const href = question ? `${to}?q=${encodeURIComponent(question)}` : to;
  return (
    <Link to={href} className={buttonVariants({ variant: "outline", size: "sm", className: "text-primary-hover [&_svg]:text-primary" })}>
      <Sparkles /> {label}
    </Link>
  );
}
