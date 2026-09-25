import { Badge, StatusBadge } from "@/components/ui/badge";
import { CellTitle, DataTable } from "@/components/ui/data-table";
import type { DepartmentComplaint, FacultySummary } from "@/types/api";
import { formatDateTime, titleCase } from "@/utils/format";

/** Department faculty: who teaches today, who is in class now, open requests. */
export function FacultyTable({ faculty }: { faculty: FacultySummary[] }) {
  return (
    <DataTable columns={["Faculty", "Classes today", "Now", "Requests"]} caption="Department faculty">
      {faculty.map((f) => (
        <tr key={f.faculty_id}>
          <td>
            <CellTitle
              title={
                <span className="inline-flex flex-wrap items-center gap-2">
                  {f.full_name} {f.is_hod && <Badge tone="primary">HOD</Badge>}
                </span>
              }
              subtitle={`${f.designation} · ${f.courses.join(", ") || "No courses"}`}
            />
          </td>
          <td className="tabular-nums">
            <span className="inline-flex flex-wrap items-center justify-end gap-2">
              {f.classes_today}
              {f.not_started_today > 0 && <StatusBadge label={`${f.not_started_today} not started`} tone="warning" />}
            </span>
          </td>
          <td>{f.active_class ? <StatusBadge label={f.active_class} tone="success" pulse /> : <span className="text-muted">—</span>}</td>
          <td className="text-sm">
            {f.own_open_requests > 0 && <div>{f.own_open_requests} open to HOD</div>}
            {f.pending_requests_to_review > 0 && <div className="text-muted">{f.pending_requests_to_review} to review</div>}
            {!f.own_open_requests && !f.pending_requests_to_review && <span className="text-muted">—</span>}
          </td>
        </tr>
      ))}
    </DataTable>
  );
}

/** Complaints with their SLA state (decided server-side). */
export function ComplaintsTable({ complaints, showStudent = true }: { complaints: DepartmentComplaint[]; showStudent?: boolean }) {
  return (
    <DataTable columns={["Case", ...(showStudent ? ["Student"] : []), "Status", "SLA"]} caption="Complaints">
      {complaints.map((c) => {
        const breached = c.response_breached || c.resolution_breached;
        return (
          <tr key={c.case_code}>
            <td>
              <CellTitle title={c.case_code} subtitle={`${titleCase(c.category)} · ${c.office}`} />
            </td>
            {showStudent && <td>{c.student_name}</td>}
            <td>
              <StatusBadge label={titleCase(c.status)} tone={c.status === "resolved" || c.status === "closed" ? "neutral" : "warning"} />
            </td>
            <td>
              {breached ? (
                <StatusBadge label={c.resolution_breached ? "Resolution breached" : "Response breached"} tone="danger" />
              ) : (
                <span className="text-sm text-muted">{c.resolution_due_at ? `Due ${formatDateTime(c.resolution_due_at)}` : "—"}</span>
              )}
            </td>
          </tr>
        );
      })}
    </DataTable>
  );
}
