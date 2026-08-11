import { useMemo, useState, type ReactNode } from "react";
import { Icon } from "./Icon";
import { EmptyState, ErrorState, Skeleton } from "./ui";
import type { IconName } from "./Icon";

export interface Column<T> {
  key: string;
  header: string;
  render?: (row: T) => ReactNode;
  sortValue?: (row: T) => string | number;
  align?: "left" | "right";
  width?: number | string;
}

export function DataTable<T extends object>({
  columns, rows, loading, error, onRetry, onRowClick, empty, rowKey, footer,
}: {
  columns: Column<T>[];
  rows: T[] | null;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  onRowClick?: (row: T) => void;
  empty?: { icon?: IconName; title: string; body?: string; action?: ReactNode };
  rowKey?: (row: T, i: number) => string;
  footer?: ReactNode;
}) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null);

  const sorted = useMemo(() => {
    if (!rows) return [];
    if (!sort) return rows;
    const col = columns.find((c) => c.key === sort.key);
    if (!col?.sortValue) return rows;
    return [...rows].sort((a, b) => {
      const va = col.sortValue!(a);
      const vb = col.sortValue!(b);
      if (va < vb) return -sort.dir;
      if (va > vb) return sort.dir;
      return 0;
    });
  }, [rows, sort, columns]);

  if (error) return <ErrorState message={error} onRetry={onRetry} />;

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                className={`${c.sortValue ? "sortable" : ""} ${c.align === "right" ? "num" : ""}`}
                style={{ width: c.width }}
                aria-sort={sort?.key === c.key ? (sort.dir === 1 ? "ascending" : "descending") : undefined}
                onClick={
                  c.sortValue
                    ? () => setSort((s) => (s?.key === c.key ? { key: c.key, dir: s.dir === 1 ? -1 : 1 } : { key: c.key, dir: 1 }))
                    : undefined
                }
              >
                <span className="row gap-4" style={{ display: "inline-flex", justifyContent: c.align === "right" ? "flex-end" : undefined }}>
                  {c.header}
                  {/* Sortable headers always reserve the chevron's slot: an icon
                      that appears on first click widens the column and makes the
                      whole table shift under the cursor. */}
                  {c.sortValue && (
                    <Icon
                      name={sort?.key === c.key && sort.dir === -1 ? "chevron-down" : "chevron-up"}
                      size={12}
                      style={{ flexShrink: 0, visibility: sort?.key === c.key ? "visible" : "hidden" }}
                    />
                  )}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {loading
            ? Array.from({ length: 5 }).map((_, r) => (
                <tr key={r}>
                  {columns.map((c) => (
                    <td key={c.key}><Skeleton h={13} w={`${55 + ((r * 17 + c.key.length * 7) % 40)}%`} /></td>
                  ))}
                </tr>
              ))
            : sorted.map((row, i) => (
                <tr
                  key={rowKey ? rowKey(row, i) : (row as { id?: string }).id ?? i}
                  className={onRowClick ? "row-click" : ""}
                  tabIndex={onRowClick ? 0 : undefined}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                  onKeyDown={onRowClick ? (e) => e.key === "Enter" && onRowClick(row) : undefined}
                >
                  {columns.map((c) => (
                    <td key={c.key} className={c.align === "right" ? "num" : ""}>
                      {c.render ? c.render(row) : String((row as Record<string, unknown>)[c.key] ?? "")}
                    </td>
                  ))}
                </tr>
              ))}
        </tbody>
      </table>
      {!loading && sorted.length === 0 && (
        <EmptyState
          icon={empty?.icon ?? "search"}
          title={empty?.title ?? "Nothing here yet"}
          body={empty?.body}
          action={empty?.action}
        />
      )}
      {footer}
    </div>
  );
}
