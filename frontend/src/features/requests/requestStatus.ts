import type { Step } from "@/components/ui/stepper";
import type { WorkflowRequest } from "@/types/api";

/** Which kind of reviewer the routing chose (from the server's routing basis). */
export function reviewStage(request: WorkflowRequest): string {
  switch (request.routing_basis) {
    case "affected_course_faculty":
    case "mentor":
      return "Faculty review";
    case "hod_escalation":
    case "department_hod":
      return "HOD review";
    case "admin_escalation":
    case "administration":
      return "Admin review";
    default:
      return "Review";
  }
}

/** Prepared → Submitted → Review → Decision, derived only from the server's status fields. */
export function requestProgress(request: WorkflowRequest): Step[] {
  const { status } = request;
  const submitted = status !== "draft" && (status !== "cancelled" || request.submitted_at != null);
  const decided = status === "approved" || status === "rejected";
  const decision: Step =
    status === "approved"
      ? { label: "Approved", state: "done" }
      : status === "rejected"
        ? { label: "Rejected", state: "failed" }
        : status === "cancelled"
          ? { label: "Withdrawn", state: "failed" }
          : { label: "Decision", state: "upcoming" };
  return [
    { label: "Prepared", state: "done" },
    { label: "Submitted", state: submitted ? "done" : status === "draft" ? "current" : "upcoming" },
    { label: reviewStage(request), state: decided ? "done" : status === "pending" || status === "needs_review" ? "current" : "upcoming" },
    decision,
  ];
}

export const requestHeading = (request: WorkflowRequest, viewer: "student" | "faculty") =>
  viewer === "faculty" && request.requester_name ? `${request.requester_name} — ${request.title}` : request.title;
