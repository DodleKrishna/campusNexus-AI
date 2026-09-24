import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LiveClassCard } from "@/components/dashboard/LiveClassCard";
import { PermissionAgentPanel } from "@/features/requests/PermissionAgentPanel";
import { FACULTY, mockApi, renderApp, signIn, withProviders } from "@/test/utils";
import type { FacultyClass, FacultyDashboard, LiveClassStatus, PermissionPreview, WorkflowRequest } from "@/types/api";

afterEach(() => vi.unstubAllGlobals());

const HEALTH = { ready: true, llm: { provider: "mock", live: false, model: null } };

const CN_CLASS: FacultyClass = {
  session_id: 7, assignment_id: 3, course_code: "CS303", course_title: "Computer Networks", department_code: "CSE", year: 3, semester: 5,
  section: "1", session_date: "2026-09-30", scheduled_start: "2026-09-30T04:30:00Z", scheduled_end: "2026-09-30T05:30:00Z",
  start_local: "10:00", end_local: "11:00", room: "Block A - Room 206", status: "scheduled", is_extra_class: false,
  actual_started_at: null, actual_closed_at: null, tally: { roster: 12, present: 0, absent: 0, late: 0, excused: 0, unmarked: 12 },
  can_start: true, start_blocked_reason: null,
};

const DASHBOARD: FacultyDashboard = {
  profile: {
    faculty_id: 4, employee_code: "EMP-CSE-004", full_name: "Dr. Ashok Verma", department_code: "CSE",
    department_name: "Computer Science & Engineering", designation: "Assistant Professor", email: "ashok.verma@meridian.edu", phone: null, assignments: [],
  },
  date: "2026-09-30", now_local: "10:05", classes_today: 1, active_class: null, students_across_today: 12, pending_requests: 1, today: [CN_CLASS],
};

const REQUEST: WorkflowRequest = {
  request_id: "REQ-1A2B3C4D", request_type: "event_permission", type_label: "Event Permission", status: "draft",
  title: "Event Permission: Competitive Coding Contest", reason: "I need permission to attend the coding contest.",
  student_id: "STU-DEMO-001", student_name: "Aditi Rao", reviewer_name: "Dr. Ashok Verma", routing_basis: "mentor",
  routing_note: "Sent to your mentor Dr. Ashok Verma (no class is affected).",
  context: {
    event: { event_id: 9, title: "Competitive Coding Contest", starts_at: "2026-10-03T09:30:00Z", ends_at: "2026-10-03T12:30:00Z", location: "Computer Lab 1" },
    request_date: "2026-10-03", day_part: null, window_start: null, window_end: null, affected_classes: [], timetable_conflict: false,
    student_name: "Aditi Rao", student_year: 3, student_section: "1", department_code: "CSE", notes: [],
  },
  created_at: "2026-09-30T04:40:00Z", submitted_at: null, decided_at: null, decided_by: null, decision_reason: null,
};

describe("Faculty dashboard", () => {
  it("shows real counts and starts a class through the API", async () => {
    signIn(FACULTY);
    const calls = mockApi((url) => {
      if (url.endsWith("/auth/me")) return { body: FACULTY };
      if (url.endsWith("/health")) return { body: HEALTH };
      if (url.endsWith("/faculty/dashboard")) return { body: DASHBOARD };
      if (url.includes("/requests?box=inbox")) return { body: [{ ...REQUEST, status: "pending", submitted_at: "2026-09-30T04:41:00Z" }] };
      if (url.endsWith("/faculty/classes/7/start")) return { body: { class_info: { ...CN_CLASS, status: "active" }, roster: [], required_percentage: 75 } };
      if (url.endsWith("/faculty/classes/7")) return { body: { class_info: { ...CN_CLASS, status: "active", actual_started_at: "2026-09-30T04:34:00Z" }, roster: [], required_percentage: 75 } };
      return undefined;
    });
    renderApp("/faculty");

    expect(await screen.findByText(/, Dr. Ashok Verma$/)).toBeInTheDocument();
    expect(screen.getByText("Students across today's classes").parentElement?.textContent).toContain("12");
    expect(await screen.findByText("Aditi Rao — Event Permission: Competitive Coding Contest")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Student Requests" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Start Class" }));
    expect(await screen.findByRole("heading", { name: "Computer Networks" })).toBeInTheDocument();
    expect(calls.some((c) => c.url.endsWith("/faculty/classes/7/start") && c.init?.method === "POST")).toBe(true);
  });
});

describe("Student live class card", () => {
  it("shows a live class and the student's attendance from the session", async () => {
    signIn();
    const live: LiveClassStatus = {
      state: "live", my_attendance: "present", my_attendance_marked_at: "2026-09-30T04:41:00Z", next_class: null,
      message: "Yes. Computer Networks started at 10:04 AM.", as_of: "2026-09-30T04:45:00Z", timezone: "Asia/Kolkata (IST)",
      current: {
        session_id: 7, course_code: "CS303", course_title: "Computer Networks", section: "1", faculty_name: "Dr. Ashok Verma",
        room: "Block A - Room 206", scheduled_start: "2026-09-30T04:30:00Z", scheduled_end: "2026-09-30T05:30:00Z", start_local: "10:00",
        end_local: "11:00", session_status: "active", is_extra_class: false, actual_started_at: "2026-09-30T04:34:00Z", actual_closed_at: null,
      },
    };
    mockApi((url) => (url.endsWith("/me/live-class") ? { body: live } : undefined));
    render(withProviders(<LiveClassCard />));
    expect(await screen.findByText("Live")).toBeInTheDocument();
    expect(screen.getByText("Computer Networks")).toBeInTheDocument();
    expect(screen.getByText("Started 10:04")).toBeInTheDocument();
    expect(screen.getByText("Present")).toBeInTheDocument();
  });
});

describe("Permission Agent", () => {
  it("previews the prepared request and sends it only on Confirm & Send", async () => {
    signIn();
    const preview: PermissionPreview = {
      outcome: "draft_ready", message: "I prepared your Event Permission: Competitive Coding Contest.", request: REQUEST, options: [], interpretation: {}, live_ai: false,
    };
    const calls = mockApi((url) => {
      if (url.endsWith("/health")) return { body: HEALTH };
      if (url.endsWith("/requests/prepare")) return { body: preview };
      if (url.endsWith("/requests")) return { body: { ...REQUEST, status: "pending", submitted_at: "2026-09-30T04:41:00Z" } };
      return undefined;
    });
    render(withProviders(<PermissionAgentPanel />));

    await userEvent.click(screen.getByRole("button", { name: "I need permission to attend the coding contest." }));
    const card = await screen.findByRole("generic", { name: "Request preview" });
    expect(within(card).getByText("Dr. Ashok Verma")).toBeInTheDocument();
    expect(within(card).getByText("Competitive Coding Contest")).toBeInTheDocument();
    expect(calls.some((c) => c.url.endsWith("/requests") && c.init?.method === "POST")).toBe(false);

    await userEvent.click(within(card).getByRole("button", { name: /Confirm & Send/ }));
    expect(await screen.findByText("pending")).toBeInTheDocument();
    const send = calls.find((c) => c.url.endsWith("/requests") && c.init?.method === "POST")!;
    // Only the draft id is sent: the reviewer and context stay server-side.
    expect(JSON.parse(String(send.init?.body))).toEqual({ request_id: "REQ-1A2B3C4D" });
  });
});
