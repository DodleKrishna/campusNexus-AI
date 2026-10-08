import type { Role } from "@/types/api";

/**
 * Starter prompts per role. Each one is something Nexus can actually do for
 * that role today: students reach the read-only specialists through Nexus;
 * staff get their own assignments, exams, attendance and missions.
 */
const SUGGESTIONS: Record<Role, string[]> = {
  student: [
    "Am I eligible to write my Operating Systems exam?",
    "Which assignments are still pending?",
    "When is my next exam?",
    "Find internships that match my skills",
    "Any workshops this week that don't clash with my classes?",
    "What's the status of my hostel complaint?",
  ],
  faculty: [
    "Who hasn't submitted my latest assignment?",
    "Summarize attendance for my classes",
    "Which exams am I running this week?",
    "What are my active missions?",
  ],
  hod: [
    "Summarize attendance across my classes",
    "Which assignments are still collecting submissions?",
    "What exams are scheduled this week?",
    "What are my active missions?",
  ],
  admin: [
    "What are my active missions?",
    "Which exams are scheduled this week?",
    "Show assignments that are still open",
    "Who am I signed in as?",
  ],
  staff: [],
};

export function suggestionsFor(role: Role): string[] {
  return SUGGESTIONS[role] ?? [];
}
