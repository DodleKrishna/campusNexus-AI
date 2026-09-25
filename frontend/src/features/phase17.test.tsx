import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentWorkspace } from "@/components/agents/AgentWorkspace";
import { FACULTY_AGENTS } from "@/features/agents/facultyCatalog";
import { WorkflowRequestCard } from "@/features/requests/WorkflowRequestCard";
import { FACULTY, STUDENT, mockApi, renderApp, signIn, withProviders } from "@/test/utils";
import type { AgentQueryResponse, AuthUser, DepartmentDashboard, WorkflowRequest } from "@/types/api";

afterEach(() => vi.unstubAllGlobals());

const HOD_USER: AuthUser = { ...STUDENT, id: 3, email: "hod@campusnexus.local", role: "hod", display_name: "Dr. Kavita Iyer", student_id: null, home_route: "/hod" };
const HEALTH = { ready: true, llm: { provider: "mock", live: false, model: null } };

const DASHBOARD: DepartmentDashboard = {
  profile: {
    faculty_id: 1, employee_code: "EMP-CSE-001", full_name: "Dr. Kavita Iyer", designation: "Professor & Head of Department",
    email: "kavita.iyer@meridian.edu", department_id: 1, department_code: "CSE", department_name: "Computer Science & Engineering",
  },
  date: "2026-09-30", now_local: "10:20", start_grace_minutes: 10, faculty_count: 6, student_count: 16, classes_today: 1,
  active_classes: 0, completed_classes: 0, not_started_classes: 1, pending_faculty_requests: 1, escalated_student_requests: 0,
  activity: [
    {
      session_id: 5, assignment_id: 4, course_code: "CS303", course_title: "Computer Networks", class_label: "CSE 3-1", year: 3, section: "1",
      faculty_id: 4, faculty_name: "Dr. Ashok Verma", room: "Block A - Room 206", scheduled_start: "2026-09-30T04:30:00Z",
      scheduled_end: "2026-09-30T05:30:00Z", start_local: "10:00", end_local: "11:00", session_status: "scheduled", state: "delayed",
      is_extra_class: false, actual_started_at: null, actual_closed_at: null, tally: null,
    },
  ],
};

const LEAVE: WorkflowRequest = {
  request_id: "REQ-FAC00001", request_type: "faculty_leave", type_label: "Faculty Leave", status: "pending",
  title: "Faculty Leave for Wed 07 Oct 2026", reason: "a conference", student_id: null, student_name: null,
  requester_kind: "faculty", requester_name: "Dr. Ashok Verma", reviewer_name: "Dr. Kavita Iyer", routing_basis: "department_hod",
  routing_note: "Sent to Dr. Kavita Iyer, HOD, CSE.",
  context: {
    event: null, request_date: "2026-10-07", day_part: "full_day", window_start: null, window_end: null, timetable_conflict: true,
    student_name: null, student_year: null, student_section: null, department_code: "CSE", requester_kind: "faculty",
    faculty_name: "Dr. Ashok Verma", faculty_designation: "Assistant Professor", faculty_employee_code: "EMP-CSE-004", notes: [],
    affected_classes: [
      {
        course_code: "CS303", course_title: "Computer Networks", section: "1", starts_at: "2026-10-07T04:30:00Z", ends_at: "2026-10-07T05:30:00Z",
        start_local: "10:00", end_local: "11:00", room: "Block A - Room 206", faculty_name: "Dr. Ashok Verma", session_status: "scheduled",
        my_mark: null, class_label: "CSE 3-1", roster_size: 12, substitute: "Not assigned", attendance_percentage: null,
        required_percentage: null, standing: "unknown",
      },
    ],
  },
  created_at: "2026-09-30T04:40:00Z", submitted_at: "2026-09-30T04:41:00Z", decided_at: null, decided_by: null, decision_reason: null,
};

describe("HOD dashboard", () => {
  it("shows department cards and the class state decided by the server", async () => {
    signIn(HOD_USER);
    mockApi((url) => {
      if (url.endsWith("/auth/me")) return { body: HOD_USER };
      if (url.endsWith("/health")) return { body: HEALTH };
      if (url.endsWith("/hod/dashboard")) return { body: DASHBOARD };
      if (url.includes("/requests?box=inbox")) return { body: [LEAVE] };
      if (url.endsWith("/faculty/notifications")) return { body: [] };
      return undefined;
    });
    renderApp("/hod");
    expect(await screen.findByRole("heading", { name: /, Dr. Kavita Iyer$/ })).toBeInTheDocument();
    expect(screen.getByText("Pending requests").parentElement?.textContent).toContain("1");
    expect(screen.getByText("Not started")).toBeInTheDocument();
    expect(await screen.findByText("Dr. Ashok Verma — Faculty Leave")).toBeInTheDocument();
    for (const label of ["Department", "Faculty", "Students", "Attendance", "Agents", "Requests", "Complaints"]) {
      expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
    }
  });
});

describe("Faculty requests", () => {
  it("shows the HOD the classes a leave would affect, with no invented substitute", () => {
    render(withProviders(<WorkflowRequestCard request={LEAVE} viewer="faculty" />));
    expect(screen.getByText("Dr. Ashok Verma — Faculty Leave for Wed 07 Oct 2026")).toBeInTheDocument();
    const row = screen.getByText("Computer Networks").closest("tr")!;
    expect(within(row).getByText("CSE 3-1")).toBeInTheDocument();
    expect(within(row).getByText("12")).toBeInTheDocument();
    expect(within(row).getByText("Not assigned")).toBeInTheDocument();
  });

  it("hands a leave request from faculty chat to the Permission Agent", async () => {
    signIn(FACULTY);
    const reply: AgentQueryResponse = {
      agent_key: "enquiry", display_name: "Enquiry Agent", verification_status: "not_applicable",
      answer: "That is a request, so it goes through the Permission Agent.", facts: { scope: "faculty", route: "permission_request", request_message: "I need leave tomorrow." },
      evidence: [], issues: [], consulted: [], notices: [], live_ai: false,
      action_hint: { agent_key: "permission", message: "That is a request, so it goes through the Permission Agent." },
    };
    const calls = mockApi((url) => {
      if (url.endsWith("/faculty/agents/enquiry/query")) return { body: reply };
      if (url.endsWith("/health")) return { body: HEALTH };
      return undefined;
    });
    render(withProviders(<AgentWorkspace agent={FACULTY_AGENTS.find((a) => a.key === "enquiry")!} userId={2} scope="faculty" />));
    await userEvent.type(screen.getByLabelText("Message Enquiry Agent"), "I need leave tomorrow.{Enter}");
    const link = await screen.findByRole("link", { name: /Open Permission Agent/ });
    expect(link).toHaveAttribute("href", "/faculty/my-requests/new?q=I%20need%20leave%20tomorrow.");
    expect(calls.some((c) => c.url.endsWith("/faculty/agents/enquiry/query"))).toBe(true);
  });
});
