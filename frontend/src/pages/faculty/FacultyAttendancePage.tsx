import { ClipboardCheck } from "lucide-react";
import { useState } from "react";
import { AskAgentLink } from "@/components/agents/AskAgentLink";
import { StatusBadge } from "@/components/ui/badge";
import { Card, CardHeader } from "@/components/ui/card";
import { CellTitle, DataTable } from "@/components/ui/data-table";
import { SearchInput, Toolbar } from "@/components/ui/input";
import { SkeletonTable } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { STANDING } from "@/components/dashboard/status";
import { useFacultyAttendance } from "@/hooks/useFacultyData";
import { PageTitle } from "@/pages/PageTitle";

/** Course attendance for every student in the faculty member's own sections (computed server-side). */
export function FacultyAttendancePage() {
  const { data, isLoading, isError, error, refetch } = useFacultyAttendance();
  const [query, setQuery] = useState("");
  const [riskOnly, setRiskOnly] = useState(false);
  const q = query.trim().toLowerCase();
  return (
    <div className="space-y-6">
      <PageTitle
        title="Attendance"
        description="Course attendance of the students you teach, against the attendance policy requirement."
        action={<AskAgentLink to="/faculty/agents/academic" label="Ask Academic Agent" />}
      />
      <Toolbar>
        <SearchInput value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search students" aria-label="Search students" />
        <label className="inline-flex items-center gap-2 text-sm text-ink">
          <input type="checkbox" checked={riskOnly} onChange={(e) => setRiskOnly(e.target.checked)} className="size-4 accent-accent" />
          At risk or below requirement only
        </label>
      </Toolbar>
      {isLoading && <SkeletonTable rows={6} />}
      {isError && <ErrorState message={`CampusNexus couldn't load attendance. ${(error as Error).message}`} onRetry={() => void refetch()} />}
      {data && data.length === 0 && (
        <Card>
          <EmptyState icon={<ClipboardCheck />} title="No teaching assignments" description="Attendance appears here for the sections you teach." />
        </Card>
      )}
      {data?.map((group) => {
        const students = group.students.filter(
          (s) => (!q || s.full_name.toLowerCase().includes(q) || s.student_id.toLowerCase().includes(q)) && (!riskOnly || s.standing === "at_risk" || s.standing === "below_requirement"),
        );
        return (
          <Card key={group.assignment.assignment_id} className="overflow-hidden">
            <CardHeader
              title={`${group.assignment.course_title} · Section ${group.assignment.section}`}
              description={`${group.assignment.course_code} · ${group.assignment.department_code} Year ${group.assignment.year} · ${group.students.length} students${group.required_percentage != null ? ` · requirement ${group.required_percentage}%` : ""}`}
              action={
                <div className="hidden flex-wrap gap-2 sm:flex">
                  {group.below_requirement > 0 && <StatusBadge label={`${group.below_requirement} below`} tone="danger" />}
                  {group.at_risk > 0 && <StatusBadge label={`${group.at_risk} at risk`} tone="warning" />}
                </div>
              }
            />
            {students.length === 0 ? (
              <EmptyState compact title="No students match" description={riskOnly ? "Nobody in this section is at risk." : "Try a different search."} />
            ) : (
              <div className="border-t border-border">
                <DataTable columns={["Student", "Attended", "Attendance", "Standing"]} caption={`${group.assignment.course_title} attendance`}>
                  {students.map((s) => (
                    <tr key={s.student_id}>
                      <td>
                        <CellTitle title={s.full_name} subtitle={s.student_id} />
                      </td>
                      <td className="tabular-nums">{s.classes_conducted != null ? `${s.classes_attended}/${s.classes_conducted}` : "—"}</td>
                      <td className="tabular-nums">{s.current_percentage != null ? `${s.current_percentage.toFixed(1)}%` : "—"}</td>
                      <td>
                        <StatusBadge {...STANDING[s.standing]} />
                      </td>
                    </tr>
                  ))}
                </DataTable>
              </div>
            )}
          </Card>
        );
      })}
    </div>
  );
}
