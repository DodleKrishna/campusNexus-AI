import { BookOpen, CalendarDays, FileCheck2, MessageSquareWarning, Sparkles } from "lucide-react";
import type { AgentDefinition } from "@/features/agents/catalog";

/** Agents an administrator can ask. Every answer is institution-wide and read-only. */
export const ADMIN_AGENTS: AgentDefinition[] = [
  {
    key: "enquiry",
    name: "Enquiry Agent",
    backendAgent: null,
    responsibility: "Institution-wide questions, answered by checking operations, attendance, requests, complaints and system health.",
    icon: Sparkles,
    available: true,
    suggestions: ["Is the campus running normally today?", "Are there any failed agent workflows today?", "Which requests are still pending?"],
  },
  {
    key: "academic",
    name: "Academic Agent",
    backendAgent: "academic_agent",
    responsibility: "Classes running or not started, and attendance risk across departments.",
    icon: BookOpen,
    available: true,
    suggestions: ["How many classes have not started today?", "Which departments have the most attendance-risk students?", "How many classes are running now?"],
  },
  {
    key: "complaints",
    name: "Complaints Agent",
    backendAgent: "campus_services_agent",
    responsibility: "Complaints across the institution and their SLA state.",
    icon: MessageSquareWarning,
    available: true,
    suggestions: ["Which complaints have breached SLA?", "Which complaints in CSE have breached SLA?"],
  },
  {
    key: "permission",
    name: "Permission Agent",
    backendAgent: null,
    responsibility: "Requests pending anywhere, and escalations to HODs and the administration.",
    icon: FileCheck2,
    available: true,
    suggestions: ["Which requests are still pending?", "Which requests have been escalated?"],
  },
  {
    key: "events",
    name: "Events Agent",
    backendAgent: "events_opportunity_agent",
    responsibility: "Upcoming campus events.",
    icon: CalendarDays,
    available: true,
    suggestions: ["Which events are coming up?"],
  },
];

export function adminAgentByKey(key: string | undefined): AgentDefinition | undefined {
  return ADMIN_AGENTS.find((agent) => agent.key === key);
}
