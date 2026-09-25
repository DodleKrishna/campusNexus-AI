import { BookOpen, Briefcase, CalendarDays, FileCheck2, MessageSquareWarning, Sparkles, type LucideIcon } from "lucide-react";
import type { AgentKey } from "@/types/api";

/**
 * The UI-facing agent catalog. Backend identifiers stay stable
 * (career_agent, campus_services_agent, ...); this is the only place that
 * maps them to the names students see.
 */
export interface AgentDefinition {
  key: AgentKey;
  /** The name used in labels ("Message Academic Agent"). */
  name: string;
  /** Optional display title for the workspace header (defaults to ``name``). */
  title?: string;
  /** One short line for headers and tiles. */
  tagline: string;
  backendAgent: string | null;
  responsibility: string;
  icon: LucideIcon;
  available: boolean;
  suggestions: string[];
}

export const AGENTS: AgentDefinition[] = [
  {
    key: "academic",
    tagline: "Attendance · Exams · Timetable · Policies",
    name: "Academic Agent",
    backendAgent: "academic_agent",
    responsibility: "Attendance, exam eligibility, timetable and exams, grounded in academic policy.",
    icon: BookOpen,
    available: true,
    suggestions: [
      "What is my OS attendance?",
      "Can I write the OS exam?",
      "How many classes do I need to attend in Operating Systems?",
      "What is my timetable today?",
      "When is my DBMS exam?",
    ],
  },
  {
    key: "events",
    tagline: "Events · Workshops · Schedule conflicts",
    name: "Events Agent",
    backendAgent: "events_opportunity_agent",
    responsibility: "Campus events and workshops, checked against your classes and exams.",
    icon: CalendarDays,
    available: true,
    suggestions: [
      "What events are available this week?",
      "Find workshops related to AI.",
      "Will the cloud workshop clash with my schedule?",
      "Am I registered for the hackathon?",
    ],
  },
  {
    key: "placements",
    tagline: "Internships · Eligibility · Skill gaps",
    name: "Placement Agent",
    backendAgent: "career_agent",
    responsibility: "Internship eligibility, skill gaps and application status.",
    icon: Briefcase,
    available: true,
    suggestions: [
      "What internships am I eligible for?",
      "What skills am I missing?",
      "What is my application status?",
      "Which companies match my profile?",
    ],
  },
  {
    key: "complaints",
    tagline: "Cases · SLA status · Escalation",
    name: "Complaints Agent",
    backendAgent: "campus_services_agent",
    responsibility: "Your grievance cases, SLA status and escalation procedure.",
    icon: MessageSquareWarning,
    available: true,
    suggestions: ["What complaints do I have?", "Are any of my complaints overdue?", "What is the grievance escalation procedure?"],
  },
  {
    key: "enquiry",
    title: "CampusNexus Assistant",
    tagline: "Ask across your campus",
    name: "Enquiry Agent",
    backendAgent: null,
    responsibility: "General campus questions, answered by consulting the other agents. Read-only.",
    icon: Sparkles,
    available: true,
    suggestions: ["Has my class started?", "Was I marked present?", "What class is next?", "Do I have anything important today?"],
  },
  {
    key: "permission",
    tagline: "Leave · OD · Event permission",
    name: "Permission Agent",
    backendAgent: null,
    responsibility: "Prepares event permission, attendance permission, leave and OD requests and routes them to the right faculty.",
    icon: FileCheck2,
    available: true,
    suggestions: [],
  },
];

export function agentByKey(key: string | undefined): AgentDefinition | undefined {
  return AGENTS.find((agent) => agent.key === key);
}
