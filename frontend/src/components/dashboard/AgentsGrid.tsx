import { ArrowRight } from "lucide-react";
import { Link } from "react-router-dom";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { AGENTS } from "@/features/agents/catalog";
import { cn } from "@/utils/cn";

export function AgentsGrid() {
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
      {AGENTS.map((agent) => {
        const body = (
          <Card className={cn("flex h-full items-start gap-4 px-5 py-4 transition-colors", agent.available && "hover:border-accent/40")}>
            <div className={cn("flex size-10 shrink-0 items-center justify-center rounded-lg", agent.available ? "bg-accent-soft text-accent-hover" : "bg-surface-muted text-subtle")}>
              <agent.icon className="size-5" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-semibold">{agent.name}</span>
                <Badge tone={agent.available ? "success" : "neutral"}>{agent.available ? "Available" : "Coming soon"}</Badge>
              </div>
              <p className="mt-1 text-xs leading-relaxed text-muted">{agent.responsibility}</p>
              {agent.available && (
                <span className="mt-2 inline-flex items-center gap-1 text-xs font-medium text-accent-hover">
                  Open <ArrowRight className="size-3.5" />
                </span>
              )}
            </div>
          </Card>
        );
        return agent.available ? (
          <Link key={agent.key} to={`/student/agents/${agent.key}`} className="block rounded-[var(--radius-card)]">
            {body}
          </Link>
        ) : (
          <div key={agent.key} aria-disabled>
            {body}
          </div>
        );
      })}
    </div>
  );
}
