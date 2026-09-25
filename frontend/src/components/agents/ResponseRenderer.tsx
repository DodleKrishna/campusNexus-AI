import { ArrowRight } from "lucide-react";
import { Link } from "react-router-dom";
import { EvidencePanel } from "@/components/agents/EvidencePanel";
import { Notice } from "@/components/ui/states";
import { agentByKey } from "@/features/agents/catalog";
import { EnquiryAnswer } from "@/features/enquiry/EnquiryAnswer";
import { FacultyAnswer } from "@/features/faculty/FacultyAnswer";
import { SpecialistFacts } from "@/features/SpecialistFacts";
import type { ChatScope } from "@/api/endpoints";
import type { AgentQueryResponse } from "@/types/api";

function hintLink(response: AgentQueryResponse, scope: ChatScope): { to: string; label: string } | null {
  const hint = response.action_hint;
  if (!hint) return null;
  if ((scope === "faculty" || scope === "hod") && hint.agent_key === "permission" && response.facts.route === "permission_request") {
    const q = typeof response.facts.request_message === "string" ? `?q=${encodeURIComponent(response.facts.request_message)}` : "";
    return { to: `/${scope}/my-requests/new${q}`, label: "Open Permission Agent" };
  }
  const agent = scope === "student" ? agentByKey(hint.agent_key) : undefined;
  return agent?.available ? { to: `/student/agents/${agent.key}`, label: `Open ${agent.name}` } : null;
}

/** An agent reply: structured cards first, then notices and next steps, evidence last (collapsed). */
export function ResponseRenderer({ response, scope = "student" }: { response: AgentQueryResponse; scope?: ChatScope }) {
  const hint = response.action_hint;
  const link = hintLink(response, scope);
  return (
    <div className="space-y-3">
      {response.facts.scope === "faculty" ? (
        <FacultyAnswer response={response} />
      ) : response.agent_key === "enquiry" || response.agent_key === "permission" ? (
        <EnquiryAnswer response={response} />
      ) : (
        <SpecialistFacts answer={{ ...response, agent_key: response.agent_key }} />
      )}
      {response.notices.map((notice) => (
        <Notice key={notice} tone="warning">
          {notice}
        </Notice>
      ))}
      {hint && (
        <Notice tone="info">
          {hint.message}
          {link && (
            <Link to={link.to} className="ml-1 inline-flex items-center gap-1 font-medium text-accent-hover hover:underline">
              {link.label} <ArrowRight className="size-3.5" />
            </Link>
          )}
        </Notice>
      )}
      <EvidencePanel
        status={response.verification_status}
        evidence={response.evidence}
        issues={response.issues}
      />
    </div>
  );
}
