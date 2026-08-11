import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Icon } from "./Icon";

/* Compact date-range filter: one select-shaped trigger opening a calendar
   popover. The popover is portalled to document.body and positioned fixed —
   the same pattern as MenuButton — so no table header, sticky bar or
   overflow-clipping card can hide it, and it repositions on scroll/resize.

   Values are local `YYYY-MM-DD` strings ("" = unbounded); interpreting those
   days as instants for the API stays with the caller. */

const pad2 = (n: number) => String(n).padStart(2, "0");
const keyOf = (d: Date) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
const dateOf = (key: string): Date | null => {
  const [y, m, d] = key.split("-").map(Number);
  return y && m && d ? new Date(y, m - 1, d) : null;
};
const DOW = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

function triggerLabel(from: string, to: string): string {
  const a = dateOf(from);
  const b = dateOf(to);
  if (!a && !b) return "All dates";
  const day = (d: Date, withYear: boolean) =>
    d.toLocaleDateString("en-GB", { day: "numeric", month: "short", ...(withYear ? { year: "numeric" } : {}) });
  if (a && b) {
    if (from === to) return day(a, true);
    return `${day(a, a.getFullYear() !== b.getFullYear())} – ${day(b, true)}`;
  }
  return a ? `From ${day(a, true)}` : `Until ${day(b!, true)}`;
}

function presets(today: Date): { label: string; from: string; to: string }[] {
  const shift = (days: number) => {
    const d = new Date(today);
    d.setDate(d.getDate() - days);
    return keyOf(d);
  };
  const now = keyOf(today);
  return [
    { label: "Today", from: now, to: now },
    { label: "Yesterday", from: shift(1), to: shift(1) },
    { label: "Last 7 days", from: shift(6), to: now },
    { label: "Last 30 days", from: shift(29), to: now },
    { label: "This month", from: keyOf(new Date(today.getFullYear(), today.getMonth(), 1)), to: now },
  ];
}

export function DateRangePicker({ from, to, max, onChange, label = "Filter by date range" }: {
  from: string;
  to: string;
  /** Latest selectable day (local `YYYY-MM-DD`), typically today. */
  max?: string;
  onChange: (from: string, to: string) => void;
  label?: string;
}) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null);
  // First click of an in-progress range; committed via onChange on the second.
  const [pending, setPending] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [month, setMonth] = useState<{ y: number; m: number }>(() => {
    const seed = dateOf(from) ?? dateOf(to) ?? new Date();
    return { y: seed.getFullYear(), m: seed.getMonth() };
  });
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  const close = () => {
    setOpen(false);
    setPosition(null);
    setPending(null);
    setHovered(null);
  };
  const openAtCurrent = () => {
    const seed = dateOf(from) ?? dateOf(to) ?? new Date();
    setMonth({ y: seed.getFullYear(), m: seed.getMonth() });
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!triggerRef.current?.contains(t) && !popRef.current?.contains(t)) close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        close();
        triggerRef.current?.focus();
      }
    };
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const anchor = triggerRef.current?.getBoundingClientRect();
      const pop = popRef.current;
      if (!anchor || !pop) return;
      const gap = 8;
      // offsetWidth/Height are transform-independent: measuring through
      // getBoundingClientRect during the pop-in scale animation reads the
      // popover a few px small and leaves it clamped against the viewport edge.
      const width = pop.offsetWidth || 380;
      const height = pop.offsetHeight || 320;
      // Right-aligned to the trigger (it sits at the toolbar's right end),
      // clamped fully inside the viewport on every side.
      const left = Math.max(gap, Math.min(
        Math.max(anchor.right - width, anchor.left),
        window.innerWidth - width - gap,
      ));
      const below = anchor.bottom + 4;
      const openUpward = height > window.innerHeight - below - gap && anchor.top - gap > window.innerHeight - below;
      const top = Math.max(gap, Math.min(
        openUpward ? anchor.top - height - 4 : below,
        window.innerHeight - height - gap,
      ));
      setPosition({ top, left });
    };
    place();
    // Re-measure once the popover has painted at its settled size — the first
    // pass can read a not-yet-final width and leave it a few px off-screen.
    const raf = requestAnimationFrame(place);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, month]);

  const apply = (a: string, b: string) => {
    onChange(a, b);
    close();
  };
  const pickDay = (key: string) => {
    if (!pending) {
      setPending(key);
      return;
    }
    // Second click completes the range; picking the earlier day second just
    // swaps the ends, so an inverted range cannot be produced.
    apply(pending <= key ? pending : key, pending <= key ? key : pending);
  };

  /* 6 fixed weeks so the popover never changes height while browsing. */
  const first = new Date(month.y, month.m, 1);
  const start = new Date(month.y, month.m, 1 - first.getDay());
  const cells = Array.from({ length: 42 }, (_, i) => {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    return d;
  });
  const monthTitle = first.toLocaleDateString("en-US", { month: "long", year: "numeric" });
  const nextDisabled = max ? keyOf(new Date(month.y, month.m + 1, 1)) > max : false;

  // While picking, preview the span between the first click and the hovered
  // day; otherwise show the committed range.
  const [lo, hi] = pending
    ? [pending, hovered ?? pending].sort()
    : [from || to, to || from];
  const active = Boolean(lo && hi);

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className={`drp-trigger${from || to ? " has-value" : ""}`}
        aria-label={label}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => (open ? close() : openAtCurrent())}
      >
        <Icon name="calendar" size={14} />
        <span className="drp-trigger-value">{triggerLabel(from, to)}</span>
        <Icon name="chevron-down" size={13} />
      </button>
      {open && createPortal(
        <div
          ref={popRef}
          className="drp-pop"
          role="dialog"
          aria-label="Choose date range"
          style={{
            position: "fixed",
            top: position?.top ?? 0,
            left: position?.left ?? 0,
            visibility: position ? "visible" : "hidden",
            zIndex: 100,
          }}
        >
          <div className="drp-presets">
            {presets(new Date()).map((p) => (
              <button key={p.label} type="button" className="drp-preset" onClick={() => apply(p.from, p.to)}>
                {p.label}
              </button>
            ))}
          </div>
          <div className="drp-cal">
            <div className="drp-head">
              <button type="button" className="drp-nav" aria-label="Previous month"
                      onClick={() => setMonth(({ y, m }) => ({ y: m ? y : y - 1, m: m ? m - 1 : 11 }))}>
                <Icon name="chevron-left" size={14} />
              </button>
              <span className="drp-month">{monthTitle}</span>
              <button type="button" className="drp-nav" aria-label="Next month" disabled={nextDisabled}
                      onClick={() => setMonth(({ y, m }) => ({ y: m === 11 ? y + 1 : y, m: m === 11 ? 0 : m + 1 }))}>
                <Icon name="chevron-right" size={14} />
              </button>
            </div>
            <div className="drp-grid" onMouseLeave={() => setHovered(null)}>
              {DOW.map((d) => <span key={d} className="drp-dow" aria-hidden="true">{d}</span>)}
              {cells.map((d) => {
                const key = keyOf(d);
                const disabled = Boolean(max && key > max);
                const inRange = active && key >= lo && key <= hi;
                const isEdge = key === lo || key === hi || key === pending;
                return (
                  <button
                    key={key}
                    type="button"
                    className={`drp-day${d.getMonth() !== month.m ? " outside" : ""}${inRange ? " in-range" : ""}${isEdge ? " edge" : ""}`}
                    disabled={disabled}
                    aria-label={d.toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" })}
                    aria-pressed={isEdge}
                    onClick={() => pickDay(key)}
                    onMouseEnter={() => setHovered(key)}
                  >
                    {d.getDate()}
                  </button>
                );
              })}
            </div>
            <div className="drp-foot">
              <span>{pending ? "Now pick the end day" : "Pick a start day"}</span>
              {(from || to || pending) && (
                <button type="button" className="drp-clear" onClick={() => apply("", "")}>
                  Clear dates
                </button>
              )}
            </div>
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}
