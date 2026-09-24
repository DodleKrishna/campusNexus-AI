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

/** A faculty agent's answer: the sentences computed server-side, plus the rows they were counted from. */
export function FacultyAnswer({ response }: { response: AgentQueryResponse }) {
  const table = asTable(response.facts.table);
  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        {response.answer
          .split("\n")
          .filter(Boolean)
          .map((line) => (
            <p key={line} className="text-sm leading-relaxed">
              {line}
            </p>
          ))}
      </div>
      {table && table.rows.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-left text-sm">
            <caption className="border-b border-border bg-surface-muted px-3 py-2 text-left text-xs font-semibold text-muted">{table.title}</caption>
            <thead>
              <tr className="border-b border-border text-xs text-muted">
                {table.columns.map((column) => (
                  <th key={column} scope="col" className="px-3 py-2 font-medium">
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {table.rows.map((row, i) => (
                <tr key={i} className="border-b border-border last:border-0">
                  {row.map((cell, j) => (
                    <td key={j} className="px-3 py-2 tabular-nums">
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
