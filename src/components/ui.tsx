import {
  useEffect, useRef, useState, type ReactNode, type ButtonHTMLAttributes, type InputHTMLAttributes,
} from "react";
import { createPortal } from "react-dom";
import { Icon, type IconName } from "./Icon";
import { Sparkline } from "./charts";
import type { Severity } from "@/types/domain";
import { useApp } from "@/state/AppContext";

/* ---------- Button ---------- */
type Variant = "primary" | "secondary" | "ghost" | "danger" | "danger-ghost";
interface BtnProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: "sm" | "md" | "lg";
  icon?: IconName;
  busy?: boolean;
}
export function Button({ variant = "secondary", size = "md", icon, busy, children, className = "", ...rest }: BtnProps) {
  const cls = `btn btn-${variant}${size !== "md" ? ` btn-${size}` : ""} ${className}`;
  return (
    <button className={cls} disabled={busy || rest.disabled} {...rest}>
      {busy ? <span className="spinner" aria-hidden /> : icon ? <Icon name={icon} size={size === "sm" ? 14 : 16} /> : null}
      {children}
    </button>
  );
}

/* ---------- Status chip ---------- */
const chipTone: Record<string, string> = {
  // bot / release lifecycle
  draft: "neutral", in_review: "info", review: "info", approved: "brand",
  published: "good", rolled_back: "serious", archived: "neutral",
  // generic
  active: "good", trial: "info", suspended: "critical", provisioning: "warning",
  paid: "good", open: "info", past_due: "critical", void: "neutral", cancelled: "neutral",
  indexed: "good", indexing: "info", failed: "critical", pending: "neutral", stale: "warning",
  healthy: "good", degraded: "warning", failing: "critical", untested: "neutral",
  live: "good", configured: "info", testing: "warning", not_configured: "neutral",
  connected: "good", available: "neutral", error: "critical",
  pending_approval: "warning",
  needs_samples: "warning", disabled: "neutral",
  acknowledged: "info", resolved: "good",
  assigned: "good", porting: "warning",
  invited: "info", deactivated: "neutral",
  deprecated: "neutral",
  positive: "good", neutral: "neutral", negative: "critical",
  good: "good", warning: "warning", serious: "serious", critical: "critical",
};
const chipIcon: Record<string, IconName> = {
  published: "check-circle", live: "check-circle", failed: "x-circle", failing: "x-circle",
  suspended: "x-circle", past_due: "alert", rolled_back: "undo", in_review: "eye",
  review: "eye", pending_approval: "clock", indexing: "refresh", provisioning: "refresh",
  testing: "refresh", stale: "clock", degraded: "alert", error: "x-circle",
};
export function StatusChip({ status, label }: { status: string; label?: string }) {
  const tone = chipTone[status] ?? "neutral";
  const icon = chipIcon[status];
  const text = label ?? status.replace(/_/g, " ");
  return (
    <span className={`chip chip-${tone}`}>
      {icon ? <Icon name={icon} size={12} /> : <span className="chip-dot" />}
      <span style={{ textTransform: "capitalize" }}>{text}</span>
    </span>
  );
}

/* ---------- Health indicator ---------- */
export function Health({ level, label }: { level: Severity; label?: string }) {
  const names: Record<Severity, string> = {
    good: "Healthy", warning: "Degraded", serious: "At risk", critical: "Critical", neutral: "No data",
  };
  return (
    <span className="health">
      <span className={`health-dot ${level}`} />
      {label ?? names[level]}
    </span>
  );
}

/* ---------- KPI card ---------- */
export function KpiCard({ label, value, delta, deltaLabel, spark, intent = "up-good", icon }: {
  label: string; value: string; delta?: number; deltaLabel?: string;
  spark?: number[]; intent?: "up-good" | "down-good"; icon?: IconName;
}) {
  const dir = delta === undefined || delta === 0 ? "flat" : delta > 0 ? "up" : "down";
  const good = delta !== undefined && ((delta > 0 && intent === "up-good") || (delta < 0 && intent === "down-good"));
  return (
    <div className="kpi">
      <div className="kpi-label">
        {icon && <Icon name={icon} size={14} />}
        {label}
      </div>
      <div className="row-between">
        <span className="kpi-value t-num">{value}</span>
        {spark && spark.length > 1 && <Sparkline data={spark} width={72} height={26} />}
      </div>
      {delta !== undefined && (
        <span className={`kpi-delta ${dir === "flat" ? "flat" : good ? "up" : "down"}`}>
          {dir !== "flat" && <Icon name={dir === "up" ? "arrow-up" : "arrow-down"} size={12} />}
          {Math.abs(delta).toFixed(1)}% {deltaLabel ?? "vs last period"}
        </span>
      )}
    </div>
  );
}

/* ---------- Empty / error / loading states ---------- */
export function EmptyState({ icon = "search", title, body, action }: {
  icon?: IconName; title: string; body?: string; action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-state-icon"><Icon name={icon} size={22} /></div>
      <div className="t-section">{title}</div>
      {body && <p className="t-sub" style={{ maxWidth: 420 }}>{body}</p>}
      {action && <div className="mt-8">{action}</div>}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="empty-state" role="alert">
      <div className="empty-state-icon error"><Icon name="alert" size={22} /></div>
      <div className="t-section">Couldn’t load this view</div>
      <p className="t-sub">{message}</p>
      {onRetry && <Button variant="secondary" icon="refresh" onClick={onRetry} className="mt-8">Try again</Button>}
    </div>
  );
}

export function Skeleton({ w = "100%", h = 14, style }: { w?: number | string; h?: number; style?: React.CSSProperties }) {
  return <div className="skeleton" style={{ width: w, height: h, ...style }} aria-hidden />;
}

export function CardSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="card card-pad col gap-12" aria-busy="true" aria-label="Loading">
      <Skeleton w="40%" h={16} />
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} w={`${88 - i * 9}%`} />
      ))}
    </div>
  );
}

/* ---------- Modal ---------- */
export function Modal({ open, onClose, title, sub, children, footer, wide }: {
  open: boolean; onClose: () => void; title: string; sub?: string;
  children: ReactNode; footer?: ReactNode; wide?: boolean;
}) {
  useEscape(open, onClose);
  if (!open) return null;
  return createPortal(
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal${wide ? " modal-lg" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-header">
          <div>
            <h2 className="t-section" style={{ fontSize: 16 }}>{title}</h2>
            {sub && <p className="t-sub mt-4">{sub}</p>}
          </div>
          <button className="btn-icon" onClick={onClose} aria-label="Close dialog"><Icon name="x" /></button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-footer">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}

/* Confirmation modal for destructive / production-impacting actions */
export function ConfirmModal({ open, onClose, onConfirm, title, body, confirmLabel = "Confirm", danger, busy }: {
  open: boolean; onClose: () => void; onConfirm: () => void;
  title: string; body: ReactNode; confirmLabel?: string; danger?: boolean; busy?: boolean;
}) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant={danger ? "danger" : "primary"} onClick={onConfirm} busy={busy}>{confirmLabel}</Button>
        </>
      }
    >
      <div className="t-sub" style={{ fontSize: 13.5 }}>{body}</div>
    </Modal>
  );
}

/* ---------- Drawer ---------- */
export function Drawer({ open, onClose, title, sub, children, footer, wide, headerExtra }: {
  open: boolean; onClose: () => void; title: ReactNode; sub?: ReactNode;
  children: ReactNode; footer?: ReactNode; wide?: boolean; headerExtra?: ReactNode;
}) {
  useEscape(open, onClose);
  if (!open) return null;
  return createPortal(
    <div className="overlay-right" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`drawer${wide ? " drawer-lg" : ""}`} role="dialog" aria-modal="true">
        <div className="drawer-header">
          <div className="grow">
            <div className="t-section" style={{ fontSize: 16 }}>{title}</div>
            {sub && <div className="t-sub mt-4">{sub}</div>}
          </div>
          {headerExtra}
          <button className="btn-icon" onClick={onClose} aria-label="Close panel"><Icon name="x" /></button>
        </div>
        <div className="drawer-body">{children}</div>
        {footer && <div className="drawer-footer">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}

function useEscape(active: boolean, onClose: () => void) {
  useEffect(() => {
    if (!active) return;
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [active, onClose]);
}

/* ---------- Tabs ---------- */
export interface TabDef {
  id: string;
  label: string;
  icon?: IconName;
  count?: number;
}
export function Tabs({ tabs, active, onChange }: { tabs: TabDef[]; active: string; onChange: (id: string) => void }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={active === t.id}
          className="tab"
          onClick={() => onChange(t.id)}
        >
          {t.icon && <Icon name={t.icon} size={14} />}
          {t.label}
          {t.count !== undefined && <span className="count t-num">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

/* ---------- Toggle ---------- */
export function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label?: string }) {
  return (
    <button
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className="switch"
      onClick={() => onChange(!checked)}
    />
  );
}

/* ---------- Progress ---------- */
export function Progress({ value, tone }: { value: number; tone?: "good" | "warning" | "critical" }) {
  return (
    <div className="progress" role="progressbar" aria-valuenow={Math.round(value)} aria-valuemin={0} aria-valuemax={100}>
      <div className={`progress-fill${tone ? ` ${tone}` : ""}`} style={{ width: `${Math.min(100, Math.max(0, value))}%` }} />
    </div>
  );
}

/* ---------- Timeline ---------- */
export interface TimelineEntry {
  icon: IconName;
  tone?: "good" | "critical" | "warning" | "brand" | "";
  title: ReactNode;
  meta: string;
  body?: ReactNode;
}
export function Timeline({ items }: { items: TimelineEntry[] }) {
  return (
    <div className="timeline">
      {items.map((it, i) => (
        <div className="timeline-item" key={i}>
          <div className={`timeline-icon ${it.tone ?? ""}`}><Icon name={it.icon} size={12} /></div>
          <div className="grow">
            <div className="t-body" style={{ fontWeight: 550 }}>{it.title}</div>
            <div className="t-micro mt-4">{it.meta}</div>
            {it.body && <div className="t-sub mt-4">{it.body}</div>}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ---------- Avatar ---------- */
const avatarHues = ["#6d55d9", "#1baf7a", "#2a78d6", "#eb6834", "#e87ba4", "#008300"];
export function Avatar({ name, size }: { name: string; size?: "lg" }) {
  const initials = name.split(" ").map((p) => p[0]).slice(0, 2).join("").toUpperCase();
  const hue = avatarHues[(name.charCodeAt(0) + name.length) % avatarHues.length];
  return (
    <span className={`avatar${size ? ` ${size}` : ""}`} style={{ background: hue }} aria-hidden>
      {initials}
    </span>
  );
}

/* ---------- Callout ---------- */
export function Callout({ tone, title, children }: { tone: "info" | "warning" | "critical" | "good"; title?: string; children: ReactNode }) {
  const icons: Record<string, IconName> = { info: "info", warning: "alert", critical: "x-circle", good: "check-circle" };
  return (
    <div className={`callout callout-${tone}`}>
      <Icon name={icons[tone]} size={15} />
      <div className="grow">
        {title && <div className="callout-title">{title}</div>}
        <div className="callout-body">{children}</div>
      </div>
    </div>
  );
}

/* ---------- Dropdown menu ---------- */
export interface MenuAction {
  label: string;
  icon?: IconName;
  danger?: boolean;
  disabled?: boolean;
  onClick: () => void;
}
export function MenuButton({ actions, label = "More actions" }: { actions: (MenuAction | "sep")[]; label?: string }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", h);
    return () => window.removeEventListener("mousedown", h);
  }, [open]);
  return (
    <div style={{ position: "relative" }} ref={ref}>
      <button className="btn-icon" aria-label={label} aria-haspopup="menu" aria-expanded={open} onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }}>
        <Icon name="more" />
      </button>
      {open && (
        <div className="menu" role="menu" style={{ right: 0, top: "calc(100% + 4px)" }}>
          {actions.map((a, i) =>
            a === "sep" ? (
              <div className="menu-sep" key={i} />
            ) : (
              <button
                key={i}
                role="menuitem"
                className={`menu-item${a.danger ? " danger" : ""}`}
                disabled={a.disabled}
                style={a.disabled ? { opacity: 0.45, cursor: "not-allowed" } : undefined}
                onClick={(e) => {
                  e.stopPropagation();
                  if (a.disabled) return;
                  setOpen(false);
                  a.onClick();
                }}
              >
                {a.icon && <Icon name={a.icon} size={14} />}
                {a.label}
              </button>
            ),
          )}
        </div>
      )}
    </div>
  );
}

/* ---------- Toast region ---------- */
export function ToastRegion() {
  const { toasts } = useApp();
  if (!toasts.length) return null;
  return createPortal(
    <div className="toast-region" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`}>
          <Icon name={t.kind === "error" ? "x-circle" : t.kind === "info" ? "info" : "check-circle"} size={15} />
          {t.message}
        </div>
      ))}
    </div>,
    document.body,
  );
}

/* ---------- Form field ---------- */
export function Field({ label, hint, error, required, plain, children }: {
  label: string; hint?: string; error?: string; required?: boolean;
  /** Render as <div> instead of <label> — needed when the child has interactive
      elements (e.g. MultiSelect chips) that a label click would activate. */
  plain?: boolean;
  children: ReactNode;
}) {
  const Tag = plain ? "div" : "label";
  return (
    <Tag className="field">
      <span className="field-label">
        {label} {required && <span className="req">*</span>}
      </span>
      {children}
      {error ? (
        <span className="field-error"><Icon name="alert" size={12} />{error}</span>
      ) : hint ? (
        <span className="field-hint">{hint}</span>
      ) : null}
    </Tag>
  );
}

/* ---------- Non-negative number input ---------- */
interface NumberInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "onChange" | "value" | "min" | "className"> {
  value: string | number;
  onChange: (value: string) => void;
  /** Lower bound (default 0 — quota/limit/price fields must never go negative). */
  min?: number;
  invalid?: boolean;
}
/** The single numeric input for limits, quotas, prices and sort orders.
    Blocks the minus key, clamps pasted/typed values below `min` (and on blur),
    and never lets the spinner decrement past `min`. */
export function NumberInput({ value, onChange, min = 0, step, invalid, onBlur, ...rest }: NumberInputProps) {
  const clamp = (raw: string): string => {
    if (raw === "" || raw === undefined) return "";
    const n = Number(raw);
    if (Number.isNaN(n)) return "";
    return n < min ? String(min) : raw;
  };
  return (
    <input
      className="input"
      type="number"
      min={min}
      step={step}
      value={String(value ?? "")}
      aria-invalid={invalid || undefined}
      onKeyDown={(e) => {
        if (min >= 0 && e.key === "-") e.preventDefault();
      }}
      onChange={(e) => onChange(clamp(e.target.value))}
      onPaste={(e) => {
        const text = e.clipboardData.getData("text");
        if (min >= 0 && text.includes("-")) {
          e.preventDefault();
          onChange(clamp(text.replace(/-/g, "")));
        }
      }}
      onBlur={(e) => {
        const clamped = clamp(e.target.value);
        if (clamped !== e.target.value) onChange(clamped);
        onBlur?.(e);
      }}
      {...rest}
    />
  );
}

/* ---------- Password input (masked by default, eye/eye-off toggle) ---------- */
interface PasswordInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "onChange" | "value" | "className"> {
  value: string;
  onChange: (value: string) => void;
  invalid?: boolean;
}
/** The single password input used everywhere a secret is typed. The visibility
    toggle is a real button (keyboard reachable, labelled), and toggling never
    touches the field's value or the surrounding form behavior. */
export function PasswordInput({ value, onChange, invalid, autoComplete = "current-password", ...rest }: PasswordInputProps) {
  const [show, setShow] = useState(false);
  return (
    <div style={{ position: "relative", width: "100%" }}>
      <input
        className="input"
        style={{ paddingRight: 38, width: "100%" }}
        type={show ? "text" : "password"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        autoComplete={autoComplete}
        aria-invalid={invalid || undefined}
        {...rest}
      />
      <button
        type="button"
        onClick={() => setShow((s) => !s)}
        aria-label={show ? "Hide password" : "Show password"}
        aria-pressed={show}
        title={show ? "Hide password" : "Show password"}
        style={{
          position: "absolute", right: 6, top: "50%", transform: "translateY(-50%)",
          display: "flex", padding: 6, background: "none", border: "none",
          cursor: "pointer", color: "var(--ink-3)",
        }}
      >
        <Icon name={show ? "eye-off" : "eye"} size={16} />
      </button>
    </div>
  );
}

/* ---------- MultiSelect (searchable, chips, "+N more" overflow) ---------- */
export interface MultiSelectOption {
  value: string;
  label: string;
  /** Secondary text shown under the label and on selected chips (e.g. a locale code). */
  sub?: string;
}

export function MultiSelect({
  options, selected, onChange, placeholder = "Select…",
  searchPlaceholder = "Search…", maxChips = 4, invalid, disabled,
}: {
  options: MultiSelectOption[];
  selected: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  searchPlaceholder?: string;
  /** Chips shown before collapsing the rest into “+N more”. */
  maxChips?: number;
  invalid?: boolean;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    // Inside scroll containers (e.g. modal bodies) the popover extends the scroll
    // height instead of floating — bring it into view.
    popRef.current?.scrollIntoView({ block: "nearest" });
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) { setOpen(false); setQuery(""); }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { setOpen(false); setQuery(""); }
    };
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("mousedown", onDown); window.removeEventListener("keydown", onKey); };
  }, [open]);

  const byValue = new Map(options.map((o) => [o.value, o]));
  const q = query.trim().toLowerCase();
  const filtered = q
    ? options.filter((o) =>
        o.value.toLowerCase().includes(q) || o.label.toLowerCase().includes(q) || (o.sub ?? "").toLowerCase().includes(q))
    : options;

  const toggle = (value: string) =>
    onChange(selected.includes(value) ? selected.filter((v) => v !== value) : [...selected, value]);

  const visible = expanded ? selected : selected.slice(0, maxChips);
  const overflow = selected.length - visible.length;

  return (
    <div className="mselect" ref={ref}>
      <div
        className={`mselect-control${open ? " open" : ""}`}
        role="combobox"
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-invalid={invalid || undefined}
        aria-disabled={disabled || undefined}
        tabIndex={disabled ? -1 : 0}
        onClick={() => !disabled && setOpen((o) => !o)}
        onKeyDown={(e) => {
          if (disabled) return;
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen((o) => !o); }
        }}
      >
        {selected.length === 0 ? (
          <span className="mselect-placeholder">{placeholder}</span>
        ) : (
          <span className="mselect-chips">
            {visible.map((v) => (
              <span key={v} className="chip chip-brand mselect-chip" title={byValue.get(v)?.label ?? v}>
                {byValue.get(v)?.sub ?? v}
                {!disabled && (
                  <button
                    type="button"
                    className="mselect-chip-x"
                    aria-label={`Remove ${byValue.get(v)?.label ?? v}`}
                    onClick={(e) => { e.stopPropagation(); toggle(v); }}
                  >
                    <Icon name="x" size={11} />
                  </button>
                )}
              </span>
            ))}
            {overflow > 0 && (
              <button type="button" className="chip chip-neutral mselect-more"
                onClick={(e) => { e.stopPropagation(); setExpanded(true); }}>
                +{overflow} more
              </button>
            )}
            {expanded && selected.length > maxChips && (
              <button type="button" className="chip chip-neutral mselect-more"
                onClick={(e) => { e.stopPropagation(); setExpanded(false); }}>
                show less
              </button>
            )}
          </span>
        )}
        <Icon name="chevron-down" size={14} className="mselect-caret" />
      </div>

      {open && (
        <div className="mselect-pop" ref={popRef}>
          <div className="mselect-search">
            <Icon name="search" size={13} />
            <input
              className="input"
              autoFocus
              value={query}
              placeholder={searchPlaceholder}
              aria-label={searchPlaceholder}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") { e.preventDefault(); if (filtered.length > 0) toggle(filtered[0].value); }
                if (e.key === "Backspace" && !query && selected.length > 0) onChange(selected.slice(0, -1));
              }}
            />
          </div>
          <div className="mselect-list" role="listbox" aria-multiselectable>
            {filtered.length === 0 && <span className="mselect-empty">No matches{query ? ` for “${query}”` : ""}</span>}
            {filtered.map((o) => {
              const on = selected.includes(o.value);
              return (
                <button key={o.value} type="button" role="option" aria-selected={on}
                  className={`mselect-option${on ? " on" : ""}`} onClick={() => toggle(o.value)}>
                  <span className={`mselect-box${on ? " on" : ""}`}>{on && <Icon name="check" size={11} />}</span>
                  <span className="mselect-opt-text">
                    <span className="mselect-opt-label">{o.label}</span>
                    {o.sub && <span className="mselect-opt-sub">{o.sub}</span>}
                  </span>
                </button>
              );
            })}
          </div>
          {selected.length > 0 && (
            <div className="mselect-foot">
              <span className="t-micro">{selected.length} selected</span>
              <button type="button" className="mselect-clear" onClick={() => onChange([])}>Clear all</button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ---------- SearchableSelect (single-select variant of MultiSelect) ---------- */
export interface SearchableSelectOption {
  value: string;
  label: string;
  /** Secondary text shown under the label (e.g. gender, locale). */
  sub?: string;
  disabled?: boolean;
}

export function SearchableSelect({
  options, value, onChange, placeholder = "Select…",
  searchPlaceholder = "Search…", invalid, disabled, ariaLabel,
}: {
  options: SearchableSelectOption[];
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
  searchPlaceholder?: string;
  invalid?: boolean;
  disabled?: boolean;
  ariaLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const ref = useRef<HTMLDivElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    popRef.current?.scrollIntoView({ block: "nearest" });
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) { setOpen(false); setQuery(""); }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { setOpen(false); setQuery(""); }
    };
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("mousedown", onDown); window.removeEventListener("keydown", onKey); };
  }, [open]);

  const q = query.trim().toLowerCase();
  const filtered = q
    ? options.filter((o) =>
        o.value.toLowerCase().includes(q) || o.label.toLowerCase().includes(q) || (o.sub ?? "").toLowerCase().includes(q))
    : options;

  useEffect(() => {
    listRef.current?.querySelector('[data-active="true"]')?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  const selectedOpt = options.find((o) => o.value === value);
  const pick = (o: SearchableSelectOption) => {
    if (o.disabled) return;
    onChange(o.value);
    setOpen(false);
    setQuery("");
  };
  const openList = () => {
    if (disabled) return;
    setActive(Math.max(0, filtered.findIndex((o) => o.value === value)));
    setOpen(true);
  };

  return (
    <div className="mselect" ref={ref}>
      <div
        className={`mselect-control${open ? " open" : ""}`}
        role="combobox"
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-invalid={invalid || undefined}
        aria-disabled={disabled || undefined}
        aria-label={ariaLabel}
        tabIndex={disabled ? -1 : 0}
        onClick={() => (open ? setOpen(false) : openList())}
        onKeyDown={(e) => {
          if (disabled) return;
          if (e.key === "Enter" || e.key === " " || e.key === "ArrowDown") {
            e.preventDefault();
            if (open && e.key !== "ArrowDown") setOpen(false);
            else openList();
          }
        }}
      >
        {selectedOpt ? (
          <span className="mselect-opt-text" style={{ flex: 1, minWidth: 0 }}>
            <span className="mselect-opt-label" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {selectedOpt.label}
            </span>
            {selectedOpt.sub && <span className="mselect-opt-sub">{selectedOpt.sub}</span>}
          </span>
        ) : (
          <span className="mselect-placeholder">{placeholder}</span>
        )}
        <Icon name="chevron-down" size={14} className="mselect-caret" />
      </div>

      {open && (
        <div className="mselect-pop" ref={popRef}>
          <div className="mselect-search">
            <Icon name="search" size={13} />
            <input
              className="input"
              autoFocus
              value={query}
              placeholder={searchPlaceholder}
              aria-label={searchPlaceholder}
              onChange={(e) => { setQuery(e.target.value); setActive(0); }}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown") { e.preventDefault(); setActive((i) => Math.min(i + 1, filtered.length - 1)); }
                if (e.key === "ArrowUp") { e.preventDefault(); setActive((i) => Math.max(i - 1, 0)); }
                if (e.key === "Enter") { e.preventDefault(); if (filtered[active]) pick(filtered[active]); }
              }}
            />
          </div>
          <div className="mselect-list" role="listbox" ref={listRef}>
            {filtered.length === 0 && <span className="mselect-empty">No matches{query ? ` for “${query}”` : ""}</span>}
            {filtered.map((o, i) => {
              const on = o.value === value;
              return (
                <button
                  key={o.value} type="button" role="option" aria-selected={on}
                  aria-disabled={o.disabled || undefined} data-active={i === active || undefined}
                  className={`mselect-option${on ? " on" : ""}`}
                  style={{
                    ...(i === active ? { background: "var(--surface-3)" } : undefined),
                    ...(o.disabled ? { opacity: 0.45, cursor: "not-allowed" } : undefined),
                  }}
                  onMouseEnter={() => setActive(i)}
                  onClick={() => pick(o)}
                >
                  <span className={`mselect-box${on ? " on" : ""}`}>{on && <Icon name="check" size={11} />}</span>
                  <span className="mselect-opt-text">
                    <span className="mselect-opt-label">{o.label}</span>
                    {o.sub && <span className="mselect-opt-sub">{o.sub}</span>}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
