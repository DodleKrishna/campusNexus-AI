import { ArrowRight } from "lucide-react";
import { Link } from "react-router-dom";
import { Badge } from "@/components/ui/badge";
import type { AgentDefinition } from "@/features/agents/catalog";
import { cn } from "@/utils/cn";

const capability = (agent: AgentDefinition) => (agent.key === "permission" ? "Prepares requests" : "Read-only");
const shortName = (agent: AgentDefinition) => agent.name.replace(/ Agent$/, "");

/** Compact module tiles for dashboards' quick access: icon and name only. */
export function AgentTiles({ agents, basePath, className }: { agents: AgentDefinition[]; basePath: string; className?: string }) {
  return (
    <div className={cn("grid grid-cols-2 gap-2 sm:grid-cols-3", className)}>
      {agents
        .filter((agent) => agent.available)
        .map((agent) => (
          <Link
            key={agent.key}
            to={`${basePath}/${agent.key}`}
            title={agent.tagline}
            aria-label={agent.name}
            className="flex min-w-0 items-center gap-2.5 rounded-md border border-border bg-surface px-3 py-2.5 text-sm font-medium text-ink transition-colors hover:border-primary/40 hover:bg-primary-soft/40"
          >
            <agent.icon aria-hidden className="size-4 shrink-0 text-primary" />
            <span className="truncate">{shortName(agent)}</span>
          </Link>
        ))}
    </div>
  );
}

/** The full agent directory for a role's Agents page. */
export function AgentDirectory({ agents, basePath }: { agents: AgentDefinition[]; basePath: string }) {
  return (
    <div className="overflow-hidden rounded-card border border-border bg-surface">
      <ul className="divide-y divide-border">
        {agents.map((agent) => {
          const body = (
            <div className="flex items-center gap-4 px-5 py-4">
              <span className={cn("flex size-9 shrink-0 items-center justify-center rounded-md", agent.available ? "bg-primary-soft text-primary" : "bg-surface-muted text-subtle")}>
                <agent.icon className="size-[18px]" />
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-sm font-semibold text-ink">{agent.title ?? agent.name}</h2>
                  <Badge tone="neutral">{agent.available ? capability(agent) : "Coming soon"}</Badge>
                </div>
                <p className="mt-0.5 text-[13px] text-muted">{agent.responsibility}</p>
              </div>
              {agent.available && <ArrowRight aria-hidden className="size-4 shrink-0 text-subtle transition-transform duration-150 group-hover:translate-x-0.5" />}
            </div>
          );
          return (
            <li key={agent.key}>
              {agent.available ? (
                <Link to={`${basePath}/${agent.key}`} className="group block transition-colors hover:bg-surface-muted/60">
                  {body}
                </Link>
              ) : (
                <div aria-disabled>{body}</div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
