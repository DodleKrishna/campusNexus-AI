import { AgentDirectory } from "@/components/agents/AgentCards";
import { RoleAgentPage } from "@/components/agents/RoleAgentPage";
import { FACULTY_AGENTS, facultyAgentByKey } from "@/features/agents/facultyCatalog";
import { PageTitle } from "@/pages/PageTitle";

export function FacultyAgentsPage() {
  return (
    <div className="space-y-6">
      <PageTitle title="Agents" description="Ask about your own classes and the requests routed to you. Agents read records; they never change attendance." />
      <AgentDirectory agents={FACULTY_AGENTS} basePath="/faculty/agents" />
    </div>
  );
}

export function FacultyAgentPage() {
  return <RoleAgentPage scope="faculty" resolve={facultyAgentByKey} basePath="/faculty/agents" />;
}
