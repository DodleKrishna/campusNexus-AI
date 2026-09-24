import { ShieldAlert, ShieldCheck, ShieldQuestion } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { VerificationStatus } from "@/types/api";

const MAP: Record<VerificationStatus, { label: string; tone: "success" | "warning" | "danger" | "neutral"; icon: typeof ShieldCheck }> = {
  verified: { label: "Verified", tone: "success", icon: ShieldCheck },
  needs_review: { label: "Partly verified", tone: "warning", icon: ShieldQuestion },
  failed: { label: "Not verified", tone: "danger", icon: ShieldAlert },
  not_applicable: { label: "No data needed", tone: "neutral", icon: ShieldQuestion },
};

export function VerificationBadge({ status }: { status: VerificationStatus }) {
  const item = MAP[status] ?? MAP.failed;
  return (
    <Badge tone={item.tone}>
      <item.icon className="size-3" /> {item.label}
    </Badge>
  );
}
