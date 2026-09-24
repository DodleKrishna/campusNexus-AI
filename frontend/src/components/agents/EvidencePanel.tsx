import { ChevronDown, FileText } from "lucide-react";
import { VerificationBadge } from "@/components/agents/VerificationBadge";
import type { Evidence, VerificationStatus } from "@/types/api";

/** Evidence and verification details, collapsed by default. */
export function EvidencePanel({ status, evidence, issues }: { status: VerificationStatus; evidence: Evidence[]; issues: string[] }) {
  const unique = evidence.filter((e, i) => evidence.findIndex((x) => x.evidence_id === e.evidence_id) === i);
  const visibleIssues = issues.filter((issue) => issue.trim());
  return (
    <details className="group rounded-lg border border-border bg-surface-muted/50 px-3 py-2">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-2 text-xs font-medium text-muted">
        <span className="flex items-center gap-2">
          Evidence & verification <VerificationBadge status={status} />
          {unique.length > 0 && <span>· {unique.length} source{unique.length > 1 ? "s" : ""}</span>}
        </span>
        <ChevronDown className="size-4 transition-transform group-open:rotate-180" />
      </summary>
      <div className="mt-3 space-y-3">
        <p className="text-xs text-muted">
          Numbers and statuses come from CampusNexus's deterministic rules over your records, then an independent verifier
          checks them before they are shown.
        </p>
        {visibleIssues.length > 0 && (
          <ul className="list-disc space-y-1 pl-5 text-xs text-warning">
            {visibleIssues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        )}
        {unique.length === 0 && <p className="text-xs text-muted">No policy documents were needed for this answer.</p>}
        {unique.map((e) => (
          <div key={e.evidence_id} className="rounded-md border border-border bg-surface px-3 py-2">
            <div className="flex items-center gap-2 text-xs font-medium">
              <FileText className="size-3.5 text-subtle" />
              {e.title}
              {e.policy_version && <span className="text-muted">({e.policy_version})</span>}
              {e.section && <span className="text-muted">· {e.section}</span>}
            </div>
            <p className="mt-1 text-xs leading-relaxed text-muted">{e.snippet}</p>
          </div>
        ))}
      </div>
    </details>
  );
}
