import { BookOpen, Bot, Briefcase, CalendarCheck2, CalendarDays, ClipboardCheck, FileCheck2, Library, MessageSquareWarning, Megaphone, ScrollText, ShieldCheck, Sparkles, UserCheck, type LucideIcon } from "lucide-react";
import type { Role } from "@/types/api";

export type AgentKind = "orchestrator" | "specialist" | "guardian" | "workflow" | "safeguard";

export interface AgentProfile {
  key: string;
  name: string;
  kind: AgentKind;
  icon: LucideIcon;
  summary: string;
  capabilities: string[];
  /** Roles for which ``prompt`` is something Nexus can actually do today. */
  promptRoles?: Role[];
  prompt?: string;
  /** The role's classic agent workspace, while it still exists. */
  classic?: Partial<Record<Role, string>>;
}

export const KIND_LABELS: Record<AgentKind, string> = {
  orchestrator: "Orchestrator",
  specialist: "Specialist · on request",
  guardian: "Guardian · autonomous",
  workflow: "Workflow",
  safeguard: "Safeguard · always on",
};

/**
 * The CAMPUS AI agent network as the people using it see it. Descriptions are
 * of what each component really does; nothing here reports a live status.
 */
export const AGENT_NETWORK: AgentProfile[] = [
  {
    key: "nexus", name: "Nexus", kind: "orchestrator", icon: Sparkles,
    summary: "Your personal assistant. Understands what you want, plans it, and coordinates the agents below.",
    capabilities: ["Text and voice", "Multi-agent planning", "Mission tracking"],
    promptRoles: ["student", "faculty", "hod", "admin"], prompt: "What are my active missions?",
  },
  {
    key: "academic", name: "Academic Agent", kind: "specialist", icon: BookOpen,
    summary: "Attendance, exam eligibility, timetable and grades — calculated by deterministic rules, grounded in policy.",
    capabilities: ["Exam eligibility", "Attendance recovery", "Timetable"],
    promptRoles: ["student"], prompt: "Am I eligible to write my Operating Systems exam?",
    classic: { student: "/student/agents/academic", faculty: "/faculty/agents/academic", hod: "/hod/agents/academic", admin: "/admin/agents/academic" },
  },
  {
    key: "career", name: "Career Agent", kind: "specialist", icon: Briefcase,
    summary: "Internships and jobs matched to your profile, eligibility checks and skill-gap guidance.",
    capabilities: ["Opportunity matching", "Eligibility", "Skill gaps"],
    promptRoles: ["student"], prompt: "Find internships that match my skills",
    classic: { student: "/student/agents/placements" },
  },
  {
    key: "events", name: "Events Agent", kind: "specialist", icon: CalendarDays,
    summary: "Workshops, clubs and competitions — checked against your classes and exams before you commit.",
    capabilities: ["Event discovery", "Clash detection", "Registration status"],
    promptRoles: ["student"], prompt: "Any workshops this week that don't clash with my classes?",
    classic: { student: "/student/agents/events", hod: "/hod/agents/events", admin: "/admin/agents/events" },
  },
  {
    key: "campus_services", name: "Campus Services Agent", kind: "specialist", icon: MessageSquareWarning,
    summary: "Hostel, fees, facilities and IT — case status, SLAs and the policy behind them.",
    capabilities: ["Complaint status", "SLA tracking", "Service policy"],
    promptRoles: ["student"], prompt: "What's the status of my hostel complaint?",
    classic: { student: "/student/agents/complaints", hod: "/hod/agents/complaints", admin: "/admin/agents/complaints" },
  },
  {
    key: "knowledge", name: "Knowledge Agent", kind: "specialist", icon: Library,
    summary: "Retrieves the official documents every policy answer must cite. If nothing relevant exists, it says so.",
    capabilities: ["Policy retrieval", "Citations", "No guessing"],
  },
  {
    key: "assignment_guardian", name: "Assignment Guardian", kind: "guardian", icon: FileCheck2,
    summary: "Watches each published assignment until every student submits or the deadline passes, and follows up within limits.",
    capabilities: ["Submission tracking", "Bounded reminders", "Deadline outcome"],
    promptRoles: ["student", "faculty", "hod", "admin"], prompt: "Which assignments are still pending?",
  },
  {
    key: "exam_guardian", name: "Exam Guardian", kind: "guardian", icon: CalendarCheck2,
    summary: "Follows a scheduled exam through attendance, absences and make-ups until every student is resolved.",
    capabilities: ["Exam attendance", "Make-up tracking", "Resolution"],
    promptRoles: ["student", "faculty", "hod", "admin"], prompt: "When is my next exam?",
  },
  {
    key: "attendance_guardian", name: "Attendance Guardian", kind: "guardian", icon: UserCheck,
    summary: "Notices confirmed absences from class roll-calls and opens a bounded intervention with the class faculty.",
    capabilities: ["Absence detection", "Interventions", "Follow-up limits"],
    promptRoles: ["student", "faculty", "hod", "admin"], prompt: "How is my attendance looking?",
  },
  {
    key: "communication", name: "Communication Agent", kind: "guardian", icon: Megaphone,
    summary: "Delivers the follow-ups other agents request — in-app, and by voice where configured — respecting quiet hours.",
    capabilities: ["In-app delivery", "Voice calls", "Quiet hours"],
  },
  {
    key: "permission", name: "Permission Agent", kind: "workflow", icon: ClipboardCheck,
    summary: "Turns a leave, OD or permission request into a draft routed to the right reviewer. You confirm before it is sent.",
    capabilities: ["Request drafting", "Deterministic routing", "Your confirmation"],
    classic: { student: "/student/requests", faculty: "/faculty/my-requests/new", hod: "/hod/my-requests/new" },
  },
  {
    key: "enquiry", name: "Enquiry Agent", kind: "workflow", icon: Bot,
    summary: "Answers campus questions from verified specialist results only, and points action requests to the right workflow.",
    capabilities: ["Verified answers", "Workflow pointers"],
    classic: { student: "/student/agents/enquiry", faculty: "/faculty/agents/enquiry", hod: "/hod/agents/enquiry", admin: "/admin/agents/enquiry" },
  },
  {
    key: "verifier", name: "Deterministic Verifier", kind: "safeguard", icon: ShieldCheck,
    summary: "Plain code — not AI — that checks every plan and action against official rules before and after it runs.",
    capabilities: ["Pre-checks", "Post-checks", "Rule formulas"],
  },
  {
    key: "approval", name: "Approval Gate & Audit", kind: "safeguard", icon: ScrollText,
    summary: "Anything irreversible, financial or sent to someone else waits for a human. Every decision is recorded.",
    capabilities: ["Human approval", "Payload binding", "Audit trail"],
  },
];
