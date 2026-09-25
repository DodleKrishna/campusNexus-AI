import { useParams } from "react-router-dom";
import { RoleAgentPage } from "@/components/agents/RoleAgentPage";
import { agentByKey } from "@/features/agents/catalog";
import { PermissionAgentPanel } from "@/features/requests/PermissionAgentPanel";

export function StudentAgentPage() {
  const { agentKey } = useParams();
  // The Permission Agent prepares a request for confirmation rather than chatting.
  if (agentKey === "permission") return <PermissionAgentPanel />;
  return <RoleAgentPage scope="student" resolve={agentByKey} basePath="/student/agents" />;
}
