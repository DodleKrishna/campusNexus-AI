import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import { FACULTY, STUDENT, mockApi, renderApp, signIn } from "@/test/utils";

const DASHBOARD = {
  profile: { student_code: "STU-DEMO-001", full_name: "Aditi Rao", first_name: "Aditi", department_code: "CSE", department_name: "Computer Science & Engineering", year: 3, semester: 5, cgpa: 7.8 },
  cgpa: 7.8,
  overall_attendance: { percentage: 82.5, classes_attended: 165, classes_conducted: 200 },
  courses_below_requirement: 1,
  next_exam: null,
  pending_requests: 0,
  unread_notifications: 0,
};

function studentApi(url: string) {
  if (url.endsWith("/auth/me")) return { body: STUDENT };
  if (url.endsWith("/me/dashboard")) return { body: DASHBOARD };
  if (url.endsWith("/health")) return { body: { ready: true, llm: { provider: "mock", live: false, model: null } } };
  if (url.includes("/me/")) return { body: url.endsWith("/timetable/today") ? { date: "2026-09-24", weekday: "Thursday", timezone: "IST", now_local: "10:15", slots: [] } : [] };
  return undefined;
}

afterEach(() => vi.unstubAllGlobals());

describe("authentication and routing", () => {
  it("sends anonymous visitors to the login page", async () => {
    mockApi(() => undefined);
    renderApp("/student");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("signs a student in and lands on the student dashboard", async () => {
    const calls = mockApi((url, init) => {
      if (url.endsWith("/auth/login") && init?.method === "POST") {
        return { body: { access_token: "t", token_type: "bearer", expires_at: new Date(Date.now() + 3_600_000).toISOString(), user: STUDENT } };
      }
      return studentApi(url);
    });
    renderApp("/login");
    await userEvent.type(screen.getByLabelText("Email"), "student@campusnexus.local");
    await userEvent.type(screen.getByLabelText("Password"), "secret");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText(/, Aditi$/)).toBeInTheDocument();
    expect(tokenStore.get()?.token).toBe("t");
    const dashboardCall = calls.find((c) => c.url.endsWith("/me/dashboard"));
    expect((dashboardCall?.init?.headers as Record<string, string>).Authorization).toBe("Bearer t");
  });

  it("shows the server's message for bad credentials", async () => {
    mockApi((url) => (url.endsWith("/auth/login") ? { status: 401, body: { detail: "Incorrect email or password." } } : undefined));
    renderApp("/login");
    await userEvent.type(screen.getByLabelText("Email"), "x@y.z");
    await userEvent.type(screen.getByLabelText("Password"), "bad");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Incorrect email or password.")).toBeInTheDocument();
  });

  it("restores a stored session after a refresh", async () => {
    signIn();
    mockApi(studentApi);
    renderApp("/student");
    expect(await screen.findByText(/, Aditi$/)).toBeInTheDocument();
  });

  it("keeps each role on its own workspace", async () => {
    signIn(FACULTY);
    mockApi((url) => (url.endsWith("/auth/me") ? { body: FACULTY } : url.endsWith("/health") ? { body: { ready: true, llm: { provider: "mock", live: false, model: null } } } : undefined));
    renderApp("/student");
    // Phase 16: a faculty member sent to a student route lands on the faculty workspace.
    expect(await screen.findByRole("link", { name: "My Classes" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "My Academics" })).not.toBeInTheDocument();
  });

  it("signs out with a clear message when the session expires", async () => {
    signIn();
    mockApi((url) => (url.endsWith("/auth/me") ? { status: 401, body: { detail: "Your session expired. Please sign in again." } } : undefined));
    renderApp("/student");
    expect(await screen.findByText("Your session expired. Please sign in again.")).toBeInTheDocument();
    await waitFor(() => expect(tokenStore.get()).toBeNull());
  });
});
