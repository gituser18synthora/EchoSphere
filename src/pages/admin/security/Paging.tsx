import { Button } from "@/components/ui";
export function Paging({ page, size, total, pages, loading, onPage, onSize }: {
  page: number; size: number; total: number; pages: number; loading: boolean;
  onPage: (value: number) => void; onSize: (value: number) => void;
}) {
  return <div className="row-between gap-12 wrap mt-12" aria-label="Pagination">
    <span className="t-sub" aria-live="polite">{total ? (page - 1) * size + 1 : 0}–{Math.min(page * size, total)} of {total}</span>
    <div className="row gap-12">
      <label>Rows <select className="select" aria-label="Rows per page" value={size} onChange={(e) => onSize(Number(e.target.value))}>
        {[25, 50, 100].map((n) => <option key={n} value={n}>{n}</option>)}</select></label>
      <Button disabled={loading || page <= 1} onClick={() => onPage(page - 1)}>Previous</Button>
      <span className="t-sub">Page {page} of {pages}</span>
      <Button disabled={loading || page >= pages} onClick={() => onPage(page + 1)}>Next</Button>
    </div>
  </div>;
}
