import { useCallback } from "react";
import { Link, Navigate, useParams, useSearchParams } from "react-router-dom";
import { useAuth } from "@/auth/useAuth";
import { AgentWorkspace } from "@/components/agents/AgentWorkspace";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/states";
import type { ChatScope } from "@/api/endpoints";
import type { AgentDefinition } from "@/features/agents/catalog";

/**
 * One agent route for every role: resolves the agent from the URL, hands over a
 * question from "Ask CampusNexus" (?q=, sent once then removed from the URL so a
 * refresh doesn't ask it again) and renders the shared workspace.
 */
export function RoleAgentPage({ scope, resolve, basePath }: { scope: ChatScope; resolve: (key: string | undefined) => AgentDefinition | undefined; basePath: string }) {
  const { agentKey } = useParams();
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();
  const agent = resolve(agentKey);
  const question = params.get("q") ?? undefined;
  const consumeQuestion = useCallback(() => setParams({}, { replace: true }), [setParams]);

  if (!agent) return <Navigate to={basePath} replace />;
  if (!user) return null;
  if (!agent.available) {
    return (
      <Card className="mx-auto w-full max-w-lg">
        <EmptyState
          icon={<agent.icon />}
          title={agent.name}
          description={`${agent.responsibility} This agent's workflow is coming in a later phase.`}
          action={
            <Link to={basePath} className="text-sm font-medium text-accent-hover hover:underline">
              Back to agents
            </Link>
          }
        />
      </Card>
    );
  }
  return <AgentWorkspace key={agent.key} agent={agent} userId={user.id} scope={scope} initialQuestion={question} onInitialQuestionSent={consumeQuestion} />;
}
