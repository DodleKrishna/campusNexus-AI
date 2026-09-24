import { Navigate, Route, Routes } from "react-router-dom";
import { RequireAuth } from "@/auth/RequireAuth";
import { homeRouteFor } from "@/auth/roles";
import { useAuth } from "@/auth/useAuth";
import { AppShell } from "@/components/layout/AppShell";
import { FullPageLoader } from "@/components/ui/full-page-loader";
import { LoginPage } from "@/pages/LoginPage";
import { NotFoundPage } from "@/pages/NotFoundPage";
import { FacultyAgentPage, FacultyAgentsPage } from "@/pages/faculty/FacultyAgentsPage";
import { FacultyAttendancePage } from "@/pages/faculty/FacultyAttendancePage";
import { FacultyClassesPage } from "@/pages/faculty/FacultyClassesPage";
import { FacultyClassPage } from "@/pages/faculty/FacultyClassPage";
import { FacultyDashboardPage } from "@/pages/faculty/FacultyDashboardPage";
import { FacultyMyRequestsPage, FacultyNewRequestPage } from "@/pages/faculty/FacultyMyRequestsPage";
import { FacultyRequestsPage } from "@/pages/faculty/FacultyRequestsPage";
import { HodDashboardPage } from "@/pages/hod/HodDashboardPage";
import {
  HodAgentPage,
  HodAgentsPage,
  HodAttendancePage,
  HodComplaintsPage,
  HodDepartmentPage,
  HodFacultyPage,
  HodRequestsPage,
  HodStudentsPage,
} from "@/pages/hod/HodPages";
import { RoleWorkspacePage } from "@/pages/RoleWorkspacePage";
import { SettingsPage } from "@/pages/SettingsPage";
import { StudentAcademicsPage } from "@/pages/student/StudentAcademicsPage";
import { StudentAgentPage } from "@/pages/student/StudentAgentPage";
import { StudentAgentsPage } from "@/pages/student/StudentAgentsPage";
import { StudentDashboardPage } from "@/pages/student/StudentDashboardPage";
import { StudentRequestsPage } from "@/pages/student/StudentRequestsPage";

function RootRedirect() {
  const { status, user } = useAuth();
  if (status === "loading") return <FullPageLoader label="Restoring your session…" />;
  return <Navigate to={user ? homeRouteFor(user.role) : "/login"} replace />;
}

function StaffRoutes({ role }: { role: "faculty" | "hod" | "admin" }) {
  return (
    <RequireAuth roles={role === "admin" ? ["admin", "staff"] : [role]}>
      <AppShell />
    </RequireAuth>
  );
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<RootRedirect />} />
      <Route path="/login" element={<LoginPage />} />

      <Route
        path="/student"
        element={
          <RequireAuth roles={["student"]}>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route index element={<StudentDashboardPage />} />
        <Route path="academics" element={<StudentAcademicsPage />} />
        <Route path="agents" element={<StudentAgentsPage />} />
        <Route path="agents/:agentKey" element={<StudentAgentPage />} />
        <Route path="requests" element={<StudentRequestsPage />} />
        <Route path="settings" element={<SettingsPage />} />
      </Route>

      <Route path="/faculty" element={<StaffRoutes role="faculty" />}>
        <Route index element={<FacultyDashboardPage />} />
        <Route path="classes" element={<FacultyClassesPage />} />
        <Route path="classes/:sessionId" element={<FacultyClassPage />} />
        <Route path="attendance" element={<FacultyAttendancePage />} />
        <Route path="agents" element={<FacultyAgentsPage />} />
        <Route path="agents/:agentKey" element={<FacultyAgentPage />} />
        <Route path="requests" element={<FacultyRequestsPage />} />
        <Route path="my-requests" element={<FacultyMyRequestsPage />} />
        <Route path="my-requests/new" element={<FacultyNewRequestPage />} />
        <Route path="settings" element={<SettingsPage />} />
      </Route>

      <Route path="/hod" element={<StaffRoutes role="hod" />}>
        <Route index element={<HodDashboardPage />} />
        <Route path="department" element={<HodDepartmentPage />} />
        <Route path="faculty" element={<HodFacultyPage />} />
        <Route path="students" element={<HodStudentsPage />} />
        <Route path="attendance" element={<HodAttendancePage />} />
        <Route path="agents" element={<HodAgentsPage />} />
        <Route path="agents/:agentKey" element={<HodAgentPage />} />
        <Route path="requests" element={<HodRequestsPage />} />
        <Route path="complaints" element={<HodComplaintsPage />} />
        <Route path="settings" element={<SettingsPage />} />
      </Route>

      {(["admin"] as const).map((role) => (
        <Route key={role} path={`/${role}`} element={<StaffRoutes role={role} />}>
          <Route index element={<RoleWorkspacePage />} />
          <Route path="settings" element={<SettingsPage />} />
        </Route>
      ))}

      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}
