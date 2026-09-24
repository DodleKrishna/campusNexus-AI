import { ArrowRight, Info } from "lucide-react";
import { Link } from "react-router-dom";
import { EvidencePanel } from "@/components/agents/EvidencePanel";
import { VerificationBadge } from "@/components/agents/VerificationBadge";
import { Notice } from "@/components/ui/states";
import { agentByKey } from "@/features/agents/catalog";
import { EnquiryAnswer } from "@/features/enquiry/EnquiryAnswer";
import { FacultyAnswer } from "@/features/faculty/FacultyAnswer";
import { SpecialistFacts } from "@/features/SpecialistFacts";
import type { AgentQueryResponse } from "@/types/api";

export function ResponseRenderer({ response }: { response: AgentQueryResponse }) {
  const hint = response.action_hint;
  const hintAgent = hint ? agentByKey(hint.agent_key) : undefined;
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <span className="text-xs font-semibold text-muted">{response.display_name}</span>
        <VerificationBadge status={response.verification_status} />
      </div>
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
          <div className="flex items-start gap-2">
            <Info className="mt-0.5 size-4 shrink-0 text-info" />
            <div>
              {hint.message}
              {hintAgent?.available && (
                <Link to={`/student/agents/${hintAgent.key}`} className="ml-1 inline-flex items-center gap-1 font-medium text-accent-hover">
                  Open {hintAgent.name} <ArrowRight className="size-3.5" />
                </Link>
              )}
            </div>
          </div>
        </Notice>
      )}
      <EvidencePanel status={response.verification_status} evidence={response.evidence} issues={response.issues} />
    </div>
  );
}
