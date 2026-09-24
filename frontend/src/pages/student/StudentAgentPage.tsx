import { useCallback } from "react";
import { Link, Navigate, useParams, useSearchParams } from "react-router-dom";
import { useAuth } from "@/auth/useAuth";
import { AgentWorkspace } from "@/components/agents/AgentWorkspace";
import { Card } from "@/components/ui/card";
import { agentByKey } from "@/features/agents/catalog";

export function StudentAgentPage() {
  const { agentKey } = useParams();
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();
  const agent = agentByKey(agentKey);
  // A question handed over by "Ask CampusNexus" (?q=) is sent once, then removed
  // from the URL so a refresh doesn't ask it again.
  const question = params.get("q") ?? undefined;
  const consumeQuestion = useCallback(() => setParams({}, { replace: true }), [setParams]);

  if (!agent) return <Navigate to="/student/agents" replace />;
  if (!user) return null;
  if (!agent.available) {
    return (
      <Card className="mx-auto max-w-lg px-6 py-8 text-center">
        <agent.icon className="mx-auto size-6 text-subtle" />
        <h1 className="mt-3 text-lg font-semibold">{agent.name}</h1>
        <p className="mt-1 text-sm text-muted">{agent.responsibility}</p>
        <p className="mt-4 text-sm">This agent's approval workflow is coming in a later phase.</p>
        <Link to="/student/agents" className="mt-4 inline-block text-sm font-medium text-accent-hover">
          Back to agents
        </Link>
      </Card>
    );
  }
  return <AgentWorkspace key={agent.key} agent={agent} userId={user.id} initialQuestion={question} onInitialQuestionSent={consumeQuestion} />;
}
