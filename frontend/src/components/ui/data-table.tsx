import { Children, cloneElement, isValidElement, type ReactElement, type ReactNode } from "react";
import { cn } from "@/utils/cn";

export type Column = string | { label: string; className?: string };

type CellProps = { children?: ReactNode; "data-label"?: string };

/** Copy each column header into its cells' data-label, which the stacked mobile layout shows. */
function labelRows(children: ReactNode, labels: string[]): ReactNode {
  return Children.map(children, (row) => {
    if (!isValidElement<{ children?: ReactNode }>(row)) return row;
    let index = 0;
    const cells = Children.map(row.props.children, (cell) => {
      if (!isValidElement<CellProps>(cell)) return cell;
      const label = labels[index++] ?? "";
      return cloneElement(cell as ReactElement<CellProps>, { "data-label": label });
    });
    return cloneElement(row, {}, cells);
  });
}

/**
 * A semantic table in the app's style: clear header, subtle row separators, no boxed
 * cells. Below 768px rows stack into labelled cards (the first column is the title),
 * so wide tables never scroll the page sideways. Keep columns to what matters.
 */
export function DataTable({
  columns,
  children,
  caption,
  flush = false,
  stack = true,
  sticky = false,
  className,
}: {
  columns: Column[];
  children: ReactNode;
  caption?: string;
  flush?: boolean;
  stack?: boolean;
  /** Keep the header visible while the page scrolls (the table must not sit in a scroll container). */
  sticky?: boolean;
  className?: string;
}) {
  const labels = columns.map((c) => (typeof c === "string" ? c : c.label));
  return (
    <div className={cn("min-w-0", !sticky && "md:overflow-x-auto", !stack && "overflow-x-auto", className)}>
      <table className="data-table" data-stack={stack ? "" : undefined} data-flush={flush ? "" : undefined} data-sticky={sticky ? "" : undefined}>
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr>
            {columns.map((c, i) => (
              <th key={`${labels[i]}-${i}`} scope="col" className={typeof c === "string" ? undefined : c.className}>
                {labels[i]}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{labelRows(children, labels)}</tbody>
      </table>
    </div>
  );
}

/** A primary-cell layout: a strong line and a muted second line. */
export function CellTitle({ title, subtitle, className }: { title: ReactNode; subtitle?: ReactNode; className?: string }) {
  return (
    <div className={cn("min-w-0", className)}>
      <div className="font-medium text-ink">{title}</div>
      {subtitle && <div className="mt-0.5 text-xs text-muted">{subtitle}</div>}
    </div>
  );
}
