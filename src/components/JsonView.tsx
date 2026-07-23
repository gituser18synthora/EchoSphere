import { useState } from "react";

/* Readable JSON viewer for metadata blobs — collapsible nodes, typed value
   colors, no external dependencies. Used instead of dumping JSON.stringify
   into a <pre> so nested chunk/KB metadata stays scannable. */

const VALUE_STYLE: Record<string, React.CSSProperties> = {
  string: { color: "var(--status-good, #1a7f37)" },
  number: { color: "var(--brand-500, #6d55d9)" },
  boolean: { color: "var(--status-warning, #b26a00)" },
  null: { color: "var(--ink-3)", fontStyle: "italic" },
};

function Value({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span style={VALUE_STYLE.null}>null</span>;
  const kind = typeof value;
  if (kind === "string") return <span style={VALUE_STYLE.string}>"{String(value)}"</span>;
  if (kind === "number") return <span style={VALUE_STYLE.number}>{String(value)}</span>;
  if (kind === "boolean") return <span style={VALUE_STYLE.boolean}>{String(value)}</span>;
  return <span>{String(value)}</span>;
}

function Node({ name, value, depth }: { name: string | null; value: unknown; depth: number }) {
  const isObject = value !== null && typeof value === "object";
  const [open, setOpen] = useState(depth < 2);

  const label = name !== null && (
    <span style={{ color: "var(--ink-2)", fontWeight: 600 }}>{name}: </span>
  );

  if (!isObject) {
    return <div style={{ paddingLeft: depth * 14 }}>{label}<Value value={value} /></div>;
  }

  const entries = Array.isArray(value)
    ? (value as unknown[]).map((v, i) => [String(i), v] as const)
    : Object.entries(value as Record<string, unknown>);
  const brackets = Array.isArray(value) ? ["[", "]"] : ["{", "}"];

  if (entries.length === 0) {
    return <div style={{ paddingLeft: depth * 14 }}>{label}{brackets[0]}{brackets[1]}</div>;
  }

  return (
    <div style={{ paddingLeft: depth * 14 }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        style={{ background: "none", border: "none", padding: 0, cursor: "pointer", font: "inherit", color: "inherit" }}
      >
        <span style={{ display: "inline-block", width: 12, color: "var(--ink-3)" }}>{open ? "▾" : "▸"}</span>
        {label}
        {brackets[0]}{!open && ` … ${brackets[1]}`}
        {!open && <span className="t-micro t-sub"> {entries.length} item{entries.length === 1 ? "" : "s"}</span>}
      </button>
      {open && (
        <>
          {entries.map(([k, v]) => <Node key={k} name={k} value={v} depth={depth + 1} />)}
          <div>{brackets[1]}</div>
        </>
      )}
    </div>
  );
}

export function JsonView({ value }: { value: unknown }) {
  return (
    <div
      style={{
        fontFamily: "var(--font-mono, ui-monospace, monospace)", fontSize: 12,
        lineHeight: 1.7, padding: 12, borderRadius: 8,
        background: "var(--surface-2, rgba(127,127,127,.08))",
        border: "1px solid var(--hairline)", overflowX: "auto",
      }}
    >
      <Node name={null} value={value} depth={0} />
    </div>
  );
}
