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

/** Keep values still present in the new schema, fill the rest with defaults. */
export function reconcileSettings(schema: Record<string, ParamSpec> | undefined, prev: ProviderSettings): ProviderSettings {
  if (!schema) return {};
  const out = schemaDefaults(schema);
  for (const [key, value] of Object.entries(prev)) {
    const spec = schema[key];
    if (spec && !spec.fixed) out[key] = value;
  }
  return out;
}

export function ParamFields({ schema, values, onChange }: {
  schema: Record<string, ParamSpec> | undefined;
  values: ProviderSettings;
  onChange: (next: ProviderSettings) => void;
}) {
  const entries = Object.entries(schema ?? {});
  if (entries.length === 0) return null;
  const basic = entries.filter(([, s]) => !s.advanced);
  const advanced = entries.filter(([, s]) => s.advanced);
  const set = (key: string, v: ProviderSettingValue | undefined) => {
    const next = { ...values };
    if (v === undefined) delete next[key];
    else next[key] = v;
    onChange(next);
  };
  return (
    <div className="col gap-12">
      {basic.map(([key, spec]) => (
        <ParamField key={key} spec={spec} value={values[key]} onChange={(v) => set(key, v)} />
      ))}
      {advanced.length > 0 && (
        <details>
          <summary className="t-label" style={{ cursor: "pointer" }}>Advanced ({advanced.length})</summary>
          <div className="col gap-12" style={{ marginTop: 10 }}>
            {advanced.map(([key, spec]) => (
              <ParamField key={key} spec={spec} value={values[key]} onChange={(v) => set(key, v)} />
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

export function ParamField({ spec, value, onChange }: {
  spec: ParamSpec; value: ProviderSettingValue | undefined;
  onChange: (v: ProviderSettingValue | undefined) => void;
}) {
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
    case "enum":
      return (
        <Field label={spec.label} plain hint={spec.help}>
          <select
            className="select" aria-label={spec.label}
            value={String(value ?? spec.default ?? "")}
            onChange={(e) => onChange(e.target.value)}
          >
            {(spec.values ?? []).map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
        </Field>
      );
    case "number":
    case "integer":
      return <NumberParam spec={spec} value={value} onChange={onChange} />;
    case "int_list":
      return <IntListParam spec={spec} value={value} onChange={onChange} />;
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
