import { lazy, type ComponentType } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { RequireAuth } from "@/auth/RequireAuth";
import { homeRouteFor } from "@/auth/roles";
import { useAuth } from "@/auth/useAuth";
import { AppShell } from "@/components/layout/AppShell";
import { AppLoader } from "@/components/ui/full-page-loader";
import { LoginPage } from "@/pages/LoginPage";
import { NotFoundPage } from "@/pages/NotFoundPage";
import { SettingsPage } from "@/pages/SettingsPage";
import { UnsupportedRolePage } from "@/pages/UnsupportedRolePage";

/** Route-level code splitting: each workspace's pages load only when first visited. */
function lazyPage<M extends Record<string, unknown>>(loader: () => Promise<M>, name: keyof M & string) {
  return lazy(() => loader().then((module) => ({ default: module[name] as ComponentType })));
}

const student = () => import("@/pages/student/StudentPages");
const StudentDashboardPage = lazyPage(student, "StudentDashboardPage");
const StudentAcademicsPage = lazyPage(student, "StudentAcademicsPage");
const StudentAgentsPage = lazyPage(student, "StudentAgentsPage");
const StudentAgentPage = lazyPage(student, "StudentAgentPage");
const StudentRequestsPage = lazyPage(student, "StudentRequestsPage");

const faculty = () => import("@/pages/faculty/FacultyPages");
const FacultyDashboardPage = lazyPage(faculty, "FacultyDashboardPage");
const FacultyClassesPage = lazyPage(faculty, "FacultyClassesPage");
const FacultyClassPage = lazyPage(faculty, "FacultyClassPage");
const FacultyAttendancePage = lazyPage(faculty, "FacultyAttendancePage");
const FacultyAgentsPage = lazyPage(faculty, "FacultyAgentsPage");
const FacultyAgentPage = lazyPage(faculty, "FacultyAgentPage");
const FacultyRequestsPage = lazyPage(faculty, "FacultyRequestsPage");
const FacultyMyRequestsPage = lazyPage(faculty, "FacultyMyRequestsPage");
const FacultyNewRequestPage = lazyPage(faculty, "FacultyNewRequestPage");

const hod = () => import("@/pages/hod/HodWorkspace");
const HodDashboardPage = lazyPage(hod, "HodDashboardPage");
const HodDepartmentPage = lazyPage(hod, "HodDepartmentPage");
const HodFacultyPage = lazyPage(hod, "HodFacultyPage");
const HodStudentsPage = lazyPage(hod, "HodStudentsPage");
const HodAttendancePage = lazyPage(hod, "HodAttendancePage");
const HodAgentsPage = lazyPage(hod, "HodAgentsPage");
const HodAgentPage = lazyPage(hod, "HodAgentPage");
const HodRequestsPage = lazyPage(hod, "HodRequestsPage");
const HodComplaintsPage = lazyPage(hod, "HodComplaintsPage");

const admin = () => import("@/pages/admin/AdminPages");
const AdminDashboardPage = lazyPage(admin, "AdminDashboardPage");
const AdminDepartmentsPage = lazyPage(admin, "AdminDepartmentsPage");
const AdminDepartmentPage = lazyPage(admin, "AdminDepartmentPage");
const AdminUsersPage = lazyPage(admin, "AdminUsersPage");
const AdminAttendancePage = lazyPage(admin, "AdminAttendancePage");
const AdminRequestsPage = lazyPage(admin, "AdminRequestsPage");
const AdminComplaintsPage = lazyPage(admin, "AdminComplaintsPage");
const AdminAIOperationsPage = lazyPage(admin, "AdminAIOperationsPage");
const AdminAuditPage = lazyPage(admin, "AdminAuditPage");
const AdminAgentsPage = lazyPage(admin, "AdminAgentsPage");
const AdminAgentPage = lazyPage(admin, "AdminAgentPage");
const AdminSettingsPage = lazyPage(admin, "AdminSettingsPage");

function RootRedirect() {
  const { status, user } = useAuth();
  if (status === "loading") return <AppLoader />;
  return <Navigate to={user ? homeRouteFor(user.role) : "/login"} replace />;
}

function StaffRoutes({ role }: { role: "faculty" | "hod" | "admin" }) {
  return (
    <RequireAuth roles={[role]}>
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
        path="/unsupported-role"
        element={
          <RequireAuth roles={["staff"]}>
            <UnsupportedRolePage />
          </RequireAuth>
        }
      />

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
        <Route path="my-requests" element={<FacultyMyRequestsPage />} />
        <Route path="my-requests/new" element={<FacultyNewRequestPage />} />
        <Route path="settings" element={<SettingsPage />} />
      </Route>

      <Route path="/admin" element={<StaffRoutes role="admin" />}>
        <Route index element={<AdminDashboardPage />} />
        <Route path="departments" element={<AdminDepartmentsPage />} />
        <Route path="departments/:code" element={<AdminDepartmentPage />} />
        <Route path="users" element={<AdminUsersPage />} />
        <Route path="attendance" element={<AdminAttendancePage />} />
        <Route path="requests" element={<AdminRequestsPage />} />
        <Route path="complaints" element={<AdminComplaintsPage />} />
        <Route path="ai-operations" element={<AdminAIOperationsPage />} />
        <Route path="audit" element={<AdminAuditPage />} />
        <Route path="agents" element={<AdminAgentsPage />} />
        <Route path="agents/:agentKey" element={<AdminAgentPage />} />
        <Route path="settings" element={<AdminSettingsPage />} />
      </Route>

      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}
