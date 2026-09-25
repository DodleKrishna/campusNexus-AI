import { DataTable } from "@/components/ui/data-table";
import type { AgentQueryResponse } from "@/types/api";

interface FactTable {
  title: string;
  columns: string[];
  rows: string[][];
}

function asTable(value: unknown): FactTable | null {
  const table = value as FactTable | null;
  if (!table || !Array.isArray(table.columns) || !Array.isArray(table.rows)) return null;
  return table;
}

/** A staff agent's answer: the sentences computed server-side, plus the rows they were counted from. */
export function FacultyAnswer({ response }: { response: AgentQueryResponse }) {
  const table = asTable(response.facts.table);
  return (
    <div className="space-y-3">
      <div className="space-y-1.5 rounded-card border border-border bg-surface px-4 py-3">
        {response.answer
          .split("\n")
          .filter(Boolean)
          .map((line) => (
            <p key={line} className="text-sm leading-relaxed text-ink">
              {line}
            </p>
          ))}
      </div>
      {table && table.rows.length > 0 && (
        <div className="overflow-hidden rounded-card border border-border bg-surface">
          <p className="border-b border-border px-4 py-2.5 text-xs font-medium text-muted">{table.title}</p>
          <DataTable columns={table.columns} caption={table.title}>
            {table.rows.map((row, i) => (
              <tr key={i}>
                {row.map((cell, j) => (
                  <td key={j} className="tabular-nums">
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </DataTable>
        </div>
      )}
    </div>
  );
}
