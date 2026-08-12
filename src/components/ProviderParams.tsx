/* Schema-driven provider parameter fields.

   Renders the `paramsSchema` of a provider model (from /providers catalog)
   into typed controls: sliders + precise numeric inputs, toggles, enums,
   integer lists and strings, with fixed parameters shown read-only and
   advanced ones collapsed. Shared by the bot Voice tab and the admin
   Add/Edit Voice form so provider rules are never duplicated. */

import { useEffect, useState } from "react";
import type { ParamSpec, ProviderSettingValue, ProviderSettings } from "@/types/domain";
import { Field, Toggle } from "@/components/ui";

export function schemaDefaults(schema: Record<string, ParamSpec> | undefined): ProviderSettings {
  const out: ProviderSettings = {};
  for (const [key, spec] of Object.entries(schema ?? {})) {
    if (spec.fixed) continue;
    if (spec.default !== undefined) out[key] = spec.default;
  }
  return out;
}

/** Coerce a carried-over value into what the NEW model's spec allows.
    Returns undefined when the value cannot be represented at all, so the
    schema default is used instead. */
function coerceToSpec(spec: ParamSpec, value: ProviderSettingValue): ProviderSettingValue | undefined {
  if (spec.type === "number" || spec.type === "integer") {
    if (typeof value !== "number") return undefined;
    const min = spec.min ?? Number.NEGATIVE_INFINITY;
    const max = spec.max ?? Number.POSITIVE_INFINITY;
    const clamped = Math.min(max, Math.max(min, value));
    return spec.type === "integer" ? Math.round(clamped) : clamped;
  }
  if (spec.type === "enum") {
    return (spec.values ?? []).includes(value as string | number) ? value : undefined;
  }
  if (spec.type === "boolean") return typeof value === "boolean" ? value : undefined;
  return value;
}

/** Keep values still present in the new schema, fill the rest with defaults.

    Values that survive a model switch are clamped into the new model's
    documented range — switching Sarvam bulbul:v2 (pace up to 3.0) to
    bulbul:v3 (max 2.0) must normalize the carried value rather than stage one
    the backend would reject. Parameters the new model does not define are
    dropped entirely, so a previous model's settings are never sent on. */
export function reconcileSettings(schema: Record<string, ParamSpec> | undefined, prev: ProviderSettings): ProviderSettings {
  if (!schema) return {};
  const out = schemaDefaults(schema);
  for (const [key, value] of Object.entries(prev)) {
    const spec = schema[key];
    if (!spec || spec.fixed) continue;
    const coerced = coerceToSpec(spec, value);
    if (coerced !== undefined) out[key] = coerced;
  }
  return out;
}

/** True when the value differs from the schema default (i.e. reset is useful). */
function isModified(spec: ParamSpec, value: ProviderSettingValue | undefined): boolean {
  if (spec.fixed) return false;
  if (value === undefined) return false;
  const fallback = spec.default;
  if (Array.isArray(value) || Array.isArray(fallback)) {
    return JSON.stringify(value) !== JSON.stringify(fallback ?? []);
  }
  return value !== fallback;
}

export function ParamFields({ schema, values, onChange, showReset = false }: {
  schema: Record<string, ParamSpec> | undefined;
  values: ProviderSettings;
  onChange: (next: ProviderSettings) => void;
  /** Render per-parameter "reset to default" affordances. */
  showReset?: boolean;
}) {
  /* Entries with a `widget` are rendered by specialized components (e.g. the
     pronunciation dictionary selector) — never as raw text inputs here. Their
     values still live in the same settings object and schema validation. */
  const entries = Object.entries(schema ?? {}).filter(([, s]) => !s.widget);
  if (entries.length === 0) return null;
  const basic = entries.filter(([, s]) => !s.advanced);
  const advanced = entries.filter(([, s]) => s.advanced);
  const set = (key: string, v: ProviderSettingValue | undefined) => {
    const next = { ...values };
    if (v === undefined) delete next[key];
    else next[key] = v;
    onChange(next);
  };
  const render = ([key, spec]: [string, ParamSpec]) => (
    <ParamField
      key={key} spec={spec} value={values[key]} onChange={(v) => set(key, v)}
      onReset={showReset && isModified(spec, values[key])
        ? () => set(key, spec.default)
        : undefined}
    />
  );
  return (
    <div className="col gap-12">
      {basic.map(render)}
      {advanced.length > 0 && (
        <details>
          <summary className="t-label" style={{ cursor: "pointer" }}>Advanced ({advanced.length})</summary>
          <div className="col gap-12" style={{ marginTop: 10 }}>
            {advanced.map(render)}
          </div>
        </details>
      )}
    </div>
  );
}

export function ParamField({ spec, value, onChange, onReset }: {
  spec: ParamSpec; value: ProviderSettingValue | undefined;
  onChange: (v: ProviderSettingValue | undefined) => void;
  /** Provided only when the value differs from the default — renders a reset. */
  onReset?: () => void;
}) {
  const control = renderParamControl(spec, value, onChange);
  if (!onReset) return control;
  return (
    <div className="param-resettable">
      {control}
      <button
        type="button" className="param-reset" onClick={onReset}
        aria-label={`Reset ${spec.label} to default`}
        title={`Reset to default (${String(spec.default ?? "—")})`}
      >
        Reset
      </button>
    </div>
  );
}

function renderParamControl(
  spec: ParamSpec,
  value: ProviderSettingValue | undefined,
  onChange: (v: ProviderSettingValue | undefined) => void,
) {
  if (spec.fixed) {
    const text = spec.type === "boolean"
      ? (spec.default ? "Always on" : "Always off")
      : `${spec.default ?? "—"} (fixed)`;
    return (
      <div className="row-between" title={spec.help}>
        <span className="field-label">{spec.label}</span>
        <span className="t-sub" style={{ fontSize: 12.5 }}>{text}</span>
      </div>
    );
  }
  switch (spec.type) {
    case "boolean":
      return (
        <div className="row-between">
          <div className="col gap-2">
            <span className="field-label">{spec.label}</span>
            {spec.help && <span className="field-hint">{spec.help}</span>}
          </div>
          <Toggle
            checked={Boolean(value ?? spec.default ?? false)}
            onChange={(v) => onChange(v)}
            label={spec.label}
          />
        </div>
      );
    case "enum": {
      /* Numeric enums (e.g. Eleven v3 stability 0.0/0.5/1.0) must stay
         numbers on the wire — the backend validates by identity against the
         schema values. Optional labels give values readable names. */
      const numeric = (spec.values ?? []).every((v) => typeof v === "number");
      const display = (v: string | number) => spec.labels?.[String(v)]
        ? `${spec.labels[String(v)]} (${v})`
        : String(v);
      return (
        <Field label={spec.label} plain hint={spec.help}>
          <select
            className="select" aria-label={spec.label}
            value={String(value ?? spec.default ?? "")}
            onChange={(e) => onChange(numeric ? Number(e.target.value) : e.target.value)}
          >
            {(spec.values ?? []).map((v) => (
              <option key={String(v)} value={String(v)}>{display(v)}</option>
            ))}
          </select>
        </Field>
      );
    }
    case "number":
    case "integer":
      return <NumberParam spec={spec} value={value} onChange={onChange} />;
    case "int_list":
      return <IntListParam spec={spec} value={value} onChange={onChange} />;
    case "string_list":
      return <StringListParam spec={spec} value={value} onChange={onChange} />;
    case "string":
    default:
      return (
        <Field label={spec.label} plain hint={spec.help}>
          <input
            className="input" aria-label={spec.label}
            value={String(value ?? spec.default ?? "")}
            maxLength={spec.max_length ?? 200}
            placeholder={spec.optional ? "optional" : undefined}
            onChange={(e) => onChange(e.target.value === "" && spec.optional ? undefined : e.target.value)}
          />
        </Field>
      );
  }
}

function NumberParam({ spec, value, onChange }: {
  spec: ParamSpec; value: ProviderSettingValue | undefined;
  onChange: (v: ProviderSettingValue) => void;
}) {
  const current = typeof value === "number" ? value
    : typeof spec.default === "number" ? spec.default
    : spec.min ?? 0;
  const min = spec.min ?? 0;
  const max = spec.max ?? 100;
  const step = spec.step ?? (spec.type === "integer" ? 1 : 0.01);
  const [text, setText] = useState(String(current));
  const [focused, setFocused] = useState(false);
  useEffect(() => { if (!focused) setText(String(current)); }, [current, focused]);
  const commit = (raw: number) => {
    if (Number.isNaN(raw)) return;
    let v = spec.type === "integer" ? Math.round(raw) : raw;
    v = Math.min(max, Math.max(min, v));
    onChange(v);
  };
  return (
    <Field label={spec.label} plain hint={spec.help}>
      <div className="row gap-12">
        <input
          type="range" min={min} max={max} step={step} value={current}
          onChange={(e) => commit(Number(e.target.value))}
          style={{ accentColor: "var(--brand-500)", flex: 1 }}
          aria-label={spec.label}
        />
        <input
          type="number" className="input" style={{ width: 92 }}
          min={min} max={max} step={step} value={text}
          aria-label={`${spec.label} value`}
          onFocus={() => setFocused(true)}
          onBlur={() => { setFocused(false); commit(Number(text)); }}
          onChange={(e) => {
            setText(e.target.value);
            const n = Number(e.target.value);
            if (e.target.value !== "" && !Number.isNaN(n) && n >= min && n <= max) commit(n);
          }}
        />
      </div>
    </Field>
  );
}

function IntListParam({ spec, value, onChange }: {
  spec: ParamSpec; value: ProviderSettingValue | undefined;
  onChange: (v: ProviderSettingValue | undefined) => void;
}) {
  const list = Array.isArray(value) ? value : Array.isArray(spec.default) ? spec.default : [];
  const joined = list.join(", ");
  const [text, setText] = useState(joined);
  const [focused, setFocused] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { if (!focused) { setText(joined); setError(null); } }, [joined, focused]);

  const handle = (raw: string) => {
    setText(raw);
    const parts = raw.split(",").map((s) => s.trim()).filter(Boolean);
    const nums: number[] = [];
    for (const p of parts) {
      if (!/^-?\d+$/.test(p)) { setError(`"${p}" is not an integer`); return; }
      nums.push(Number(p));
    }
    if (spec.max_items !== undefined && nums.length > spec.max_items) {
      setError(`At most ${spec.max_items} entries allowed`);
      return;
    }
    const lo = spec.min ?? Number.NEGATIVE_INFINITY;
    const hi = spec.max ?? Number.POSITIVE_INFINITY;
    const bad = nums.find((n) => n < lo || n > hi);
    if (bad !== undefined) {
      setError(`${bad} is outside ${spec.min ?? "-∞"}–${spec.max ?? "∞"}`);
      return;
    }
    setError(null);
    onChange(nums.length === 0 && spec.optional ? undefined : nums);
  };

  return (
    <Field label={spec.label} hint={error ? undefined : (spec.help ?? "Comma-separated integers")} error={error ?? undefined}>
      <input
        className="input" value={text} aria-label={spec.label} aria-invalid={error ? true : undefined}
        placeholder="e.g. 1, 2, 3"
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        onChange={(e) => handle(e.target.value)}
      />
    </Field>
  );
}

function StringListParam({ spec, value, onChange }: {
  spec: ParamSpec; value: ProviderSettingValue | undefined;
  onChange: (v: ProviderSettingValue | undefined) => void;
}) {
  const list = Array.isArray(value) ? value : Array.isArray(spec.default) ? spec.default : [];
  const joined = list.join(", ");
  const [text, setText] = useState(joined);
  const [focused, setFocused] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { if (!focused) { setText(joined); setError(null); } }, [joined, focused]);

  const allowed = (spec.values ?? []).map(String);
  const handle = (raw: string) => {
    setText(raw);
    const parts = raw.split(",").map((s) => s.trim()).filter(Boolean);
    if (spec.max_items !== undefined && parts.length > spec.max_items) {
      setError(`At most ${spec.max_items} entries allowed`);
      return;
    }
    if (allowed.length > 0) {
      const bad = parts.find((p) => !allowed.includes(p));
      if (bad !== undefined) {
        setError(`"${bad}" is not one of: ${allowed.join(", ")}`);
        return;
      }
    }
    setError(null);
    onChange(parts.length === 0 ? undefined : parts);
  };

  return (
    <Field
      label={spec.label}
      hint={error ? undefined : (spec.help ?? "Comma-separated values")}
      error={error ?? undefined}
    >
      <input
        className="input" value={text} aria-label={spec.label} aria-invalid={error ? true : undefined}
        placeholder={allowed.length > 0 ? `e.g. ${allowed.slice(0, 2).join(", ")}` : "e.g. a, b"}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        onChange={(e) => handle(e.target.value)}
      />
    </Field>
  );
}
