import { ChevronDown, FileText } from "lucide-react";
import type { Evidence, VerificationStatus } from "@/types/api";

/** Reasoning inputs, sources and verifier notes: collapsed by default, never the lead. */
export function EvidencePanel({ status, evidence, issues, label = "Sources & verification" }: { status: VerificationStatus; evidence: Evidence[]; issues: string[]; label?: string }) {
  const unique = evidence.filter((e, i) => evidence.findIndex((x) => x.evidence_id === e.evidence_id) === i);
  const visibleIssues = issues.filter((issue) => issue.trim());
  return (
    <details className="group">
      <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 rounded-md text-sm font-medium text-muted transition-colors hover:text-ink">
        {label}
        {unique.length > 0 && <span className="font-normal">· {unique.length} source{unique.length > 1 ? "s" : ""}</span>}
        <ChevronDown aria-hidden className="size-4 transition-transform duration-150 group-open:rotate-180" />
      </summary>
      <div className="mt-3 space-y-3 rounded-lg border border-border bg-surface px-4 py-3">
        <p className="text-sm text-muted">
          {status === "not_applicable"
            ? "This answer did not need a calculation or a policy document."
            : "Numbers and statuses come from CampusNexus's deterministic rules over your records; an independent verifier checks them before they are shown."}
        </p>
        {visibleIssues.length > 0 && (
          <ul className="list-disc space-y-1 pl-5 text-sm text-warning">
            {visibleIssues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        )}
        {unique.length === 0 && <p className="text-sm text-muted">No policy documents were needed for this answer.</p>}
        {unique.map((e) => (
          <div key={e.evidence_id} className="border-t border-border pt-3">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-sm font-medium text-ink">
              <FileText aria-hidden className="size-4 text-subtle" />
              {e.title}
              {e.policy_version && <span className="font-normal text-muted">({e.policy_version})</span>}
              {e.section && <span className="font-normal text-muted">· {e.section}</span>}
            </div>
            <p className="mt-1 text-sm leading-relaxed text-muted">{e.snippet}</p>
          </div>
        ))}
      </div>
    </details>
  );
}
