import { BookOpen, FileCheck2, Sparkles } from "lucide-react";
import type { AgentDefinition } from "@/features/agents/catalog";

/** Agents a faculty member can ask. Every answer is scoped server-side to their own classes and requests. */
export const FACULTY_AGENTS: AgentDefinition[] = [
  {
    key: "academic",
    tagline: "Your classes, presence and attendance risk",
    name: "Academic Agent",
    backendAgent: "academic_agent",
    responsibility: "Your classes today, who is present or absent, and who is below the attendance requirement.",
    icon: BookOpen,
    available: true,
    suggestions: [
      "How many classes do I have today?",
      "How many students are present in my current class?",
      "Who is absent?",
      "Which students have attendance below 75%?",
    ],
  },
  {
    key: "enquiry",
    title: "CampusNexus Assistant",
    tagline: "Ask about your classes and requests",
    name: "Enquiry Agent",
    backendAgent: null,
    responsibility: "Any question about your own classes and student requests, answered from live records. Read-only.",
    icon: Sparkles,
    available: true,
    suggestions: ["Which class is happening now?", "How many students are present in CSE 3rd year Section 1?", "What is my next class?"],
  },
  {
    key: "permission",
    tagline: "Student requests waiting for you",
    name: "Permission Agent",
    backendAgent: null,
    responsibility: "Student permission, leave and OD requests waiting for your decision.",
    icon: FileCheck2,
    available: true,
    suggestions: ["Which student requests are pending?"],
  },
];

export function facultyAgentByKey(key: string | undefined): AgentDefinition | undefined {
  return FACULTY_AGENTS.find((agent) => agent.key === key);
}
