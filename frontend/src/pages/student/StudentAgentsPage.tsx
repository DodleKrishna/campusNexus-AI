import { AgentDirectory } from "@/components/agents/AgentCards";
import { AGENTS } from "@/features/agents/catalog";
import { PageTitle } from "@/pages/PageTitle";

export function StudentAgentsPage() {
  return (
    <div className="space-y-6">
      <PageTitle title="Agents" description="Each agent reads your records, applies campus rules deterministically and explains the verified result." />
      <AgentDirectory agents={AGENTS} basePath="/student/agents" />
    </div>
  );
}
