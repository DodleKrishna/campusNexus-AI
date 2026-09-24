import { Users } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { STANDING } from "@/components/dashboard/status";
import { useFacultyAttendance } from "@/hooks/useFacultyData";
import { PageTitle } from "@/pages/PageTitle";

/** Course attendance for every student in the faculty member's own sections (computed server-side). */
export function FacultyAttendancePage() {
  const { data, isLoading, isError, error, refetch } = useFacultyAttendance();
  return (
    <div className="space-y-6">
      <PageTitle title="Attendance" description="Course attendance of the students you teach, against the attendance policy requirement." />
      {isLoading && <SkeletonRows rows={4} />}
      {isError && <ErrorState message={`CampusNexus couldn't load attendance. ${(error as Error).message}`} onRetry={() => void refetch()} />}
      {data && data.length === 0 && <EmptyState title="No teaching assignments" />}
      {data?.map((group) => (
        <Card key={group.assignment.assignment_id}>
          <CardHeader
            icon={<Users />}
            title={`${group.assignment.course_title} · Section ${group.assignment.section}`}
            description={`${group.assignment.course_code} · ${group.assignment.department_code} Year ${group.assignment.year} · ${group.students.length} students${group.required_percentage != null ? ` · requirement ${group.required_percentage}%` : ""}`}
            action={
              <div className="flex gap-2">
                {group.below_requirement > 0 && <Badge tone="danger">{group.below_requirement} below</Badge>}
                {group.at_risk > 0 && <Badge tone="warning">{group.at_risk} at risk</Badge>}
              </div>
            }
          />
          <CardBody>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead>
                  <tr className="border-b border-border text-xs text-muted">
                    <th className="py-2 pr-3 font-medium">Student</th>
                    <th className="py-2 pr-3 font-medium">Attended</th>
                    <th className="py-2 pr-3 font-medium">Attendance</th>
                    <th className="py-2 font-medium">Standing</th>
                  </tr>
                </thead>
                <tbody>
                  {group.students.map((s) => (
                    <tr key={s.student_id} className="border-b border-border last:border-0">
                      <td className="py-2 pr-3">
                        <div className="font-medium">{s.full_name}</div>
                        <div className="text-xs text-muted">{s.student_id}</div>
                      </td>
                      <td className="py-2 pr-3 tabular-nums">{s.classes_conducted != null ? `${s.classes_attended}/${s.classes_conducted}` : "—"}</td>
                      <td className="py-2 pr-3 tabular-nums">{s.current_percentage != null ? `${s.current_percentage.toFixed(1)}%` : "—"}</td>
                      <td className="py-2">
                        <Badge tone={STANDING[s.standing].tone}>{STANDING[s.standing].label}</Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardBody>
        </Card>
      ))}
    </div>
  );
}
