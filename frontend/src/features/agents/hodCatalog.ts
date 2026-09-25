import { BookOpen, CalendarDays, FileCheck2, MessageSquareWarning, Sparkles } from "lucide-react";
import type { AgentDefinition } from "@/features/agents/catalog";

/** Agents a head of department can ask. Every answer is scoped server-side to their own department. */
export const HOD_AGENTS: AgentDefinition[] = [
  {
    key: "enquiry",
    title: "CampusNexus Assistant",
    tagline: "Ask across your department",
    name: "Enquiry Agent",
    backendAgent: null,
    responsibility: "Broad department questions, answered by checking operations, attendance, requests and complaints. Read-only.",
    icon: Sparkles,
    available: true,
    suggestions: ["Is everything running normally in CSE today?", "Which classes haven't started?", "Which students are below 75% attendance?"],
  },
  {
    key: "academic",
    tagline: "Classes, delays and attendance by section",
    name: "Academic Agent",
    backendAgent: "academic_agent",
    responsibility: "Classes running, delayed or not held, and attendance by section and course.",
    icon: BookOpen,
    available: true,
    suggestions: [
      "How many classes are running now?",
      "Which faculty are teaching today?",
      "Which classes did not start on time?",
      "Which course has the lowest attendance?",
      "How many students were present in CSE 3rd year Section 1 today?",
    ],
  },
  {
    key: "permission",
    tagline: "Faculty and escalated requests",
    name: "Permission Agent",
    backendAgent: null,
    responsibility: "Faculty requests and escalated student requests waiting for your decision.",
    icon: FileCheck2,
    available: true,
    suggestions: ["Which faculty requests are pending?", "Are any student requests escalated to me?"],
  },
  {
    key: "complaints",
    tagline: "Department complaints and SLA state",
    name: "Complaints Agent",
    backendAgent: "campus_services_agent",
    responsibility: "Complaints from your department's students and their SLA state.",
    icon: MessageSquareWarning,
    available: true,
    suggestions: ["Which complaints in CSE have breached SLA?"],
  },
  {
    key: "events",
    tagline: "Upcoming events and registrations",
    name: "Events Agent",
    backendAgent: "events_opportunity_agent",
    responsibility: "Upcoming campus events and how many of your students registered.",
    icon: CalendarDays,
    available: true,
    suggestions: ["Which events are coming up?"],
  },
];

export function hodAgentByKey(key: string | undefined): AgentDefinition | undefined {
  return HOD_AGENTS.find((agent) => agent.key === key);
}
