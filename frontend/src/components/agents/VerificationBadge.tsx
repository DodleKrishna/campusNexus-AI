import { StatusBadge } from "@/components/ui/badge";
import type { StatusInfo } from "@/components/dashboard/status";
import type { VerificationStatus } from "@/types/api";

const MAP: Record<VerificationStatus, StatusInfo> = {
  verified: { label: "Verified", tone: "success" },
  needs_review: { label: "Partly verified", tone: "warning" },
  failed: { label: "Not verified", tone: "danger" },
  not_applicable: { label: "No data needed", tone: "neutral" },
};

export function VerificationBadge({ status }: { status: VerificationStatus }) {
  return <StatusBadge {...(MAP[status] ?? MAP.failed)} />;
}
