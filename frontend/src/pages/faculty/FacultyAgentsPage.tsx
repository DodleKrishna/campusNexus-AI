import { ArrowRight } from "lucide-react";
import { Link, Navigate, useParams } from "react-router-dom";
import { useAuth } from "@/auth/useAuth";
import { AgentWorkspace } from "@/components/agents/AgentWorkspace";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { FACULTY_AGENTS, facultyAgentByKey } from "@/features/agents/facultyCatalog";
import { PageTitle } from "@/pages/PageTitle";

export function FacultyAgentsPage() {
  return (
    <div className="space-y-6">
      <PageTitle title="Agents" description="Ask about your own classes and the requests routed to you. Agents read records; they never change attendance." />
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {FACULTY_AGENTS.map((agent) => (
          <Link key={agent.key} to={`/faculty/agents/${agent.key}`} className="block rounded-[var(--radius-card)]">
            <Card className="flex h-full items-start gap-4 px-5 py-4 transition-colors hover:border-accent/40">
              <div className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-accent-soft text-accent-hover">
                <agent.icon className="size-5" />
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-semibold">{agent.name}</span>
                  <Badge tone="info">Read-only</Badge>
                </div>
                <p className="mt-1 text-xs leading-relaxed text-muted">{agent.responsibility}</p>
                <span className="mt-2 inline-flex items-center gap-1 text-xs font-medium text-accent-hover">
                  Open <ArrowRight className="size-3.5" />
                </span>
              </div>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  );
}

export function FacultyAgentPage() {
  const { agentKey } = useParams();
  const { user } = useAuth();
  const agent = facultyAgentByKey(agentKey);
  if (!agent) return <Navigate to="/faculty/agents" replace />;
  if (!user) return null;
  return <AgentWorkspace key={agent.key} agent={agent} userId={user.id} scope="faculty" />;
}
