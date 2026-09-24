import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { AuthProvider } from "@/auth/AuthProvider";
import { tokenStore } from "@/auth/tokenStore";
import { AppRoutes } from "@/routes/AppRoutes";
import type { AuthUser } from "@/types/api";

export const STUDENT: AuthUser = {
  id: 1, email: "student@campusnexus.local", role: "student", display_name: "Aditi Rao", student_id: "STU-DEMO-001",
  department_code: "CSE", department_name: "Computer Science & Engineering", home_route: "/student",
};
export const FACULTY: AuthUser = { ...STUDENT, id: 2, email: "faculty@campusnexus.local", role: "faculty", display_name: "Dr. Ashok Verma", student_id: null, home_route: "/faculty" };

type Handler = (url: string, init?: RequestInit) => { status?: number; body?: unknown } | undefined;

/** Stub fetch with a route table; unmatched calls return 404. Returns the call log. */
export function mockApi(handler: Handler) {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, init });
      const result = handler(url, init) ?? { status: 404, body: { detail: "Not found" } };
      const status = result.status ?? 200;
      return new Response(status === 204 ? null : JSON.stringify(result.body ?? {}), {
        status, headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

export function signIn(user: AuthUser = STUDENT) {
  tokenStore.set({ token: `token-for-${user.id}`, expiresAt: new Date(Date.now() + 3_600_000).toISOString() });
}

export function renderApp(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <AuthProvider>
          <AppRoutes />
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

export function withProviders(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}
