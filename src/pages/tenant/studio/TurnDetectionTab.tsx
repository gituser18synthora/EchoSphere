import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { Button, Callout, CardSkeleton, ConfirmModal, ErrorState, Modal, Toggle } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { useAsync } from "@/hooks/useAsync";
import { getTurnDetectionSettings, saveTurnDetectionSettings } from "@/services/api";
import { useApp } from "@/state/AppContext";
import {
  buildTurnDetectionExport,
  parseTurnDetectionImport,
} from "./turnDetectionTransfer";
import type { TurnDetectionImportResult } from "./turnDetectionTransfer";
import type { IconName } from "@/components/Icon";
import type {
  TurnDetectionConfig,
  TurnDetectionField,
  TurnDetectionMode,
  TurnDetectionOverrides,
  TurnDetectionTransport,
} from "@/types/domain";

interface Draft {
  mode: TurnDetectionMode;
  overrides: TurnDetectionOverrides;
}

const MODE_ICONS: Record<TurnDetectionMode, IconName> = {
  system_default: "settings",
  recommended: "sparkles",
  custom: "sliders",
};
const TRANSPORT_ICONS: Record<TurnDetectionTransport, IconName> = {
  browser: "mic",
  telephony: "phone",
};

const cloneOverrides = (value: TurnDetectionOverrides): TurnDetectionOverrides =>
  JSON.parse(JSON.stringify(value ?? {})) as TurnDetectionOverrides;

/** Key-order-independent serialization so reordered-but-equal drafts never
    read as unsaved changes. */
const stable = (value: unknown): string =>
  JSON.stringify(value, (_key, val) =>
    val && typeof val === "object" && !Array.isArray(val)
      ? Object.fromEntries(Object.entries(val as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)))
      : val);

const fmt = (value: number): string =>
  Number.isInteger(value) ? String(value) : String(Number(value.toFixed(4)));

function fieldValue(
  field: TurnDetectionField,
  transport: TurnDetectionTransport,
  draft: Draft,
): number {
  if (draft.mode === "recommended") return field.recommended[transport];
  if (draft.mode === "custom") {
    const override = draft.overrides[transport]?.[field.group]?.[field.key];
    if (override !== undefined) return typeof override === "boolean" ? Number(override) : override;
  }
  return field.default[transport];
}

function compactOverrides(overrides: TurnDetectionOverrides): TurnDetectionOverrides {
  const next = cloneOverrides(overrides);
  for (const transport of Object.keys(next) as TurnDetectionTransport[]) {
    const groups = next[transport];
    if (!groups) continue;
    for (const group of ["turn_detection", "noise_gate"] as const) {
      if (groups[group] && Object.keys(groups[group]!).length === 0) delete groups[group];
    }
    if (Object.keys(groups).length === 0) delete next[transport];
  }
  return next;
}

/** Snapshot of what the user currently sees, as sparse overrides — used when
    a non-custom view is edited so Custom starts from the visible values. */
function materializeEffective(config: TurnDetectionConfig, draft: Draft): TurnDetectionOverrides {
  const next: TurnDetectionOverrides = {};
  for (const transport of config.transports.map((item) => item.id)) {
    for (const field of config.fields) {
      const value = fieldValue(field, transport, draft);
      if (value === field.default[transport]) continue;
      const transportValues = (next[transport] ??= {});
      const groupValues = (transportValues[field.group] ??= {});
      groupValues[field.key] = field.valueType === "boolean" ? Boolean(value) : value;
    }
  }
  return next;
}

function validationErrors(config: TurnDetectionConfig, draft: Draft): string[] {
  if (draft.mode !== "custom") return [];
  const fields = new Map(config.fields.map((field) => [`${field.group}.${field.key}`, field]));
  const errors: string[] = [];
  for (const [transport, groups] of Object.entries(draft.overrides)) {
    for (const [group, values] of Object.entries(groups ?? {})) {
      for (const [key, raw] of Object.entries(values ?? {})) {
        const field = fields.get(`${group}.${key}`);
        const value = typeof raw === "boolean" ? Number(raw) : Number(raw);
        if (!field || !Number.isFinite(value) || value < field.min || value > field.max) {
          errors.push(`${transport} · ${field?.label ?? key} must be within its allowed range.`);
        } else if (field.valueType === "integer" && !Number.isInteger(value)) {
          errors.push(`${transport} · ${field.label} must be a whole number.`);
        }
      }
    }
  }
  return errors;
}

/** Purely metadata-driven risk heuristic: warn when a value sits at an extreme
    of its allowed range AND far from the recommended profile. No per-field or
    per-tenant rules — the schema's bounds and recommendations are the source. */
function riskFor(
  field: TurnDetectionField,
  transport: TurnDetectionTransport,
  value: number,
): string | null {
  if (field.valueType === "boolean") return null;
  const span = field.max - field.min;
  if (span <= 0) return null;
  const position = (value - field.min) / span;
  const recommendedPosition = (field.recommended[transport] - field.min) / span;
  if (position <= 0.07 && recommendedPosition - position >= 0.08) {
    return `Extreme low value — well below the recommended ${fmt(field.recommended[transport])} ${field.unit}. `
      + "Extreme settings can cause premature cutoff, missed speech, or unstable barge-in.";
  }
  if (position >= 0.93 && position - recommendedPosition >= 0.08) {
    return `Extreme high value — well above the recommended ${fmt(field.recommended[transport])} ${field.unit}. `
      + "Extreme settings can delay responses, slow endpointing, or make interruptions unreliable.";
  }
  return null;
}

type ValueState = "default" | "recommended" | "custom";

function valueState(
  field: TurnDetectionField,
  transport: TurnDetectionTransport,
  value: number,
): ValueState {
  if (value === field.default[transport]) return "default";
  if (value === field.recommended[transport]) return "recommended";
  return "custom";
}

const STATE_CHIP: Record<ValueState, { cls: string; label: string }> = {
  default: { cls: "chip chip-neutral", label: "Default" },
  recommended: { cls: "chip chip-info", label: "Recommended" },
  custom: { cls: "chip chip-brand", label: "Custom" },
};

const MONO_TEXT: CSSProperties = {
  fontFamily: "var(--font-mono, ui-monospace, monospace)",
  fontSize: 12.5,
  lineHeight: 1.5,
  resize: "vertical",
};

const fmtFieldValue = (field: TurnDetectionField, value: number): string =>
  field.valueType === "boolean" ? (value ? "On" : "Off") : fmt(value);

export default function TurnDetectionTab() {
  const { toast } = useApp();
  const settingsQ = useAsync(getTurnDetectionSettings, []);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [transport, setTransport] = useState<TurnDetectionTransport>("browser");
  const [saving, setSaving] = useState(false);
  const [serverErrors, setServerErrors] = useState<string[]>([]);
  const [confirmResetAll, setConfirmResetAll] = useState(false);
  /** In-progress text for numeric inputs, keyed by transport.group.key — lets
      the user type freely; the draft itself only ever holds clamped numbers. */
  const [editText, setEditText] = useState<Record<string, string>>({});
  /** Last custom overrides seen, so switching mode away and back within one
      editing session never silently destroys unsaved custom values. */
  const stashRef = useRef<TurnDetectionOverrides | null>(null);
  /** Copy/Import: JSON shown when the clipboard is unavailable, and the
      paste → validate → preview → apply state of the import modal. */
  const [exportFallback, setExportFallback] = useState<string | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState("");
  const [importResult, setImportResult] = useState<TurnDetectionImportResult | null>(null);
  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);

  useEffect(() => {
    if (settingsQ.data) {
      setDraft({
        mode: settingsQ.data.mode,
        overrides: cloneOverrides(settingsQ.data.overrides),
      });
      setServerErrors([]);
      setEditText({});
    }
  }, [settingsQ.data]);

  const config = settingsQ.data;
  const localErrors = useMemo(
    () => (config && draft ? validationErrors(config, draft) : []),
    [config, draft],
  );
  const dirty = useMemo(
    () =>
      Boolean(
        config && draft
        && stable({ mode: draft.mode, overrides: compactOverrides(draft.overrides) })
          !== stable({ mode: config.mode, overrides: config.overrides }),
      ),
    [config, draft],
  );

  if (settingsQ.error) return <ErrorState message={settingsQ.error} onRetry={settingsQ.reload} />;
  if (settingsQ.loading || !config || !draft) return <CardSkeleton rows={10} />;

  const savedMode = config.modes.find((mode) => mode.id === config.mode);

  const chooseMode = (mode: TurnDetectionMode) => {
    if (mode === draft.mode) return;
    setServerErrors([]);
    setEditText({});
    if (draft.mode === "custom") stashRef.current = cloneOverrides(draft.overrides);
    if (mode === "custom") {
      const stashed = stashRef.current && Object.keys(stashRef.current).length > 0
        ? cloneOverrides(stashRef.current)
        : materializeEffective(config, draft);
      setDraft({ mode, overrides: stashed });
    } else {
      setDraft({ mode, overrides: {} });
    }
  };

  /** Clamp + store a value; any edit moves the draft into Custom mode, seeded
      with exactly the values that were on screen. */
  const commitField = (field: TurnDetectionField, raw: number | boolean) => {
    const normalized = typeof raw === "boolean"
      ? raw
      : Math.min(field.max, Math.max(field.min,
        field.valueType === "integer" ? Math.round(raw) : raw));
    const base = draft.mode === "custom" ? cloneOverrides(draft.overrides) : materializeEffective(config, draft);
    const transportValues = (base[transport] ??= {});
    const groupValues = (transportValues[field.group] ??= {});
    groupValues[field.key] = normalized;
    setDraft({ mode: "custom", overrides: compactOverrides(base) });
    setServerErrors([]);
  };

  const resetSection = (sectionId: string) => {
    if (draft.mode === "system_default") return;
    const base = draft.mode === "custom" ? cloneOverrides(draft.overrides) : materializeEffective(config, draft);
    for (const field of config.fields.filter((item) => item.section === sectionId)) {
      delete base[transport]?.[field.group]?.[field.key];
    }
    setDraft({ mode: "custom", overrides: compactOverrides(base) });
    setEditText({});
    setServerErrors([]);
  };

  const resetAll = () => {
    stashRef.current = draft.mode === "custom" ? cloneOverrides(draft.overrides) : stashRef.current;
    setDraft({ mode: "system_default", overrides: {} });
    setEditText({});
    setServerErrors([]);
    setConfirmResetAll(false);
  };

  const discard = () => {
    setDraft({ mode: config.mode, overrides: cloneOverrides(config.overrides) });
    setEditText({});
    setServerErrors([]);
  };

  const save = async () => {
    const problems = validationErrors(config, draft);
    if (problems.length) {
      setServerErrors(problems);
      toast("Turn detection values are outside the allowed range", "error");
      return;
    }
    setSaving(true);
    setServerErrors([]);
    try {
      const saved = await saveTurnDetectionSettings(draft.mode, compactOverrides(draft.overrides));
      setDraft({ mode: saved.mode, overrides: cloneOverrides(saved.overrides) });
      setEditText({});
      toast("Turn detection settings saved — new voice sessions will use them");
      settingsQ.reload();
    } catch (error) {
      setServerErrors([error instanceof Error ? error.message : "Could not save turn detection settings."]);
      toast("Could not save turn detection settings", "error");
    } finally {
      setSaving(false);
    }
  };

  /** Portable export of what is currently on screen — mode plus sparse
      overrides only, never tenant, bot or database identifiers. */
  const copyConfiguration = async () => {
    const text = buildTurnDetectionExport(config, draft.mode, compactOverrides(draft.overrides));
    try {
      await navigator.clipboard.writeText(text);
      toast(dirty
        ? "Configuration copied — includes your unsaved changes"
        : "Turn detection configuration copied to clipboard");
    } catch {
      setExportFallback(text);
    }
  };

  const openImport = () => {
    setImportText("");
    setImportResult(null);
    setApplyError(null);
    setImportOpen(true);
  };

  /** Apply goes through the normal save API, so the backend re-validates the
      whole document and refreshes the runtime snapshot cache; a rejected
      import leaves the stored configuration untouched. */
  const applyImport = async () => {
    const document = importResult?.document;
    if (!document) return;
    setApplying(true);
    setApplyError(null);
    try {
      const saved = await saveTurnDetectionSettings(document.mode, document.overrides);
      setDraft({ mode: saved.mode, overrides: cloneOverrides(saved.overrides) });
      stashRef.current = null;
      setEditText({});
      setServerErrors([]);
      setImportOpen(false);
      toast("Imported configuration applied — new voice sessions will use it");
      settingsQ.reload();
    } catch (error) {
      setApplyError(error instanceof Error ? error.message : "Could not apply the imported configuration.");
    } finally {
      setApplying(false);
    }
  };

  const previewDocument = importResult?.document ?? null;
  const previewChanges = previewDocument
    ? config.transports.map((item) => ({
      transport: item,
      rows: config.fields.flatMap((field) => {
        const current = config.effective[item.id]?.[field.group]?.[field.key] ?? field.default[item.id];
        const imported = fieldValue(field, item.id, previewDocument);
        return current === imported ? [] : [{ field, current, imported }];
      }),
    }))
    : [];
  const previewChangeCount = previewChanges.reduce((sum, item) => sum + item.rows.length, 0);
  const previewMode = previewDocument ? config.modes.find((mode) => mode.id === previewDocument.mode) : undefined;

  const activeTransport = config.transports.find((item) => item.id === transport)!;
  const modifiedInSection = (sectionId: string): number =>
    config.fields.filter(
      (field) => field.section === sectionId
        && fieldValue(field, transport, draft) !== field.default[transport],
    ).length;
  const modifiedInTransport = (id: TurnDetectionTransport): number =>
    config.fields.filter((field) => fieldValue(field, id, draft) !== field.default[id]).length;

  return (
    <div className="col gap-16 td-page">
      {/* ── Header ── */}
      <div className="row-between gap-12 wrap" style={{ alignItems: "flex-start" }}>
        <div className="col gap-4" style={{ maxWidth: 780 }}>
          <div className="row gap-8 wrap">
            <h2 className="t-title" style={{ fontSize: 18, margin: 0 }}>Turn Detection</h2>
            <span className="chip chip-neutral" title="These settings are shared by every bot in this workspace">
              <Icon name="building" size={12} /> Tenant-wide
            </span>
          </div>
          <p className="t-sub" style={{ margin: 0 }}>
            Controls when the bot decides a caller has started or stopped speaking, and how caller
            interruptions (barge-in) are handled — with independent profiles for browser microphones
            and telephony calls.
          </p>
          <p className="t-micro" style={{ margin: 0 }}>
            <Icon name="zap" size={11} style={{ verticalAlign: -1 }} /> Loaded once at session start —
            active calls keep their settings and no lookups happen inside the live audio path.
          </p>
        </div>
        <div className="col gap-10" style={{ alignItems: "flex-end" }}>
          <div className="col gap-4" style={{ alignItems: "flex-end" }}>
            <span className="t-label">Active configuration</span>
            <span className={config.mode === "system_default" ? "chip chip-neutral" : config.mode === "recommended" ? "chip chip-info" : "chip chip-brand"}>
              <Icon name={MODE_ICONS[config.mode]} size={12} />
              {savedMode?.label ?? config.mode}
            </span>
          </div>
          <div className="row gap-8 wrap" style={{ justifyContent: "flex-end" }}>
            <Button
              size="sm"
              icon="copy"
              title="Copy the configuration shown on this page as portable JSON — no tenant or bot identifiers"
              onClick={() => void copyConfiguration()}
            >
              Copy Configuration
            </Button>
            <Button
              size="sm"
              icon="upload"
              title="Paste a configuration copied from another bot or workspace, preview it and apply it"
              onClick={openImport}
            >
              Import Configuration
            </Button>
          </div>
        </div>
      </div>

      {/* ── Mode selector ── */}
      <section className="card">
        <div className="card-header">
          <div className="col gap-2">
            <span className="card-title">Configuration mode</span>
            <span className="t-micro">Pick a base profile — adjusting any value below switches to Custom automatically.</span>
          </div>
        </div>
        <div className="grid grid-3" style={{ padding: 16, gap: 12 }} role="radiogroup" aria-label="Configuration mode">
          {config.modes.map((mode) => (
            <button
              key={mode.id}
              type="button"
              role="radio"
              aria-checked={draft.mode === mode.id}
              className="td-mode-card"
              onClick={() => chooseMode(mode.id)}
            >
              <div className="row-between gap-8">
                <span className="t-strong row gap-6" style={{ fontSize: 13.5 }}>
                  <Icon name={MODE_ICONS[mode.id]} size={15} />
                  {mode.label}
                </span>
                {config.mode === mode.id && <span className="chip chip-good">Active</span>}
              </div>
              <span className="t-micro" style={{ textAlign: "left" }}>{mode.description}</span>
            </button>
          ))}
        </div>
      </section>

      {/* ── Transport switcher ── */}
      <div className="col gap-6">
        <div className="row gap-8 wrap" role="tablist" aria-label="Audio transport">
          {config.transports.map((item) => {
            const modified = modifiedInTransport(item.id);
            return (
              <button
                key={item.id}
                type="button"
                role="tab"
                aria-selected={transport === item.id}
                aria-label={item.label}
                className="td-transport-tab"
                onClick={() => { setTransport(item.id); setEditText({}); }}
              >
                <Icon name={TRANSPORT_ICONS[item.id]} size={15} />
                {item.label}
                {modified > 0 && <span className="td-count" title={`${modified} settings differ from system default`}>{modified}</span>}
              </button>
            );
          })}
        </div>
        <p className="t-micro" style={{ margin: 0 }}>{activeTransport.description}</p>
      </div>

      {/* ── Sections ── */}
      {config.sections.map((section) => {
        const fields = config.fields.filter((field) => field.section === section.id);
        if (!fields.length) return null;
        const modified = modifiedInSection(section.id);
        const resettable = draft.mode !== "system_default" && modified > 0;
        return (
          <section className="card" key={section.id}>
            <div className="card-header">
              <div className="col gap-2">
                <span className="card-title">{section.label}</span>
                <span className="t-micro">{section.description}</span>
              </div>
              <div className="row gap-8">
                {modified > 0 && (
                  <span className="chip chip-brand" title={`${modified} settings differ from system default on ${activeTransport.label}`}>
                    {modified} modified
                  </span>
                )}
                <Button
                  size="sm"
                  variant="ghost"
                  icon="undo"
                  disabled={!resettable}
                  title={`Return ${section.label} to system defaults for ${activeTransport.label}`}
                  onClick={() => resetSection(section.id)}
                >
                  Reset Section
                </Button>
              </div>
            </div>
            <div className="grid grid-2" style={{ padding: 16, gap: 12 }}>
              {fields.map((field) => {
                const value = fieldValue(field, transport, draft);
                const state = valueState(field, transport, value);
                const chip = STATE_CHIP[state];
                const risk = riskFor(field, transport, value);
                const textKey = `${transport}.${field.group}.${field.key}`;
                const text = editText[textKey];
                const typedInvalid = text !== undefined && text !== ""
                  && (!Number.isFinite(Number(text)) || Number(text) < field.min || Number(text) > field.max);
                return (
                  <div className="td-field" key={`${field.group}.${field.key}`}>
                    <div className="row-between gap-8">
                      <span className="field-label" title={`Internal key: ${field.group}.${field.key}`}>
                        {field.label}
                      </span>
                      <span className={chip.cls}>{chip.label}</span>
                    </div>
                    <span className="field-hint">{field.description}</span>
                    {field.input === "toggle" ? (
                      <div className="row gap-8 mt-4">
                        <Toggle checked={Boolean(value)} onChange={(next) => commitField(field, next)} label={field.label} />
                        <span className="t-sub">{value ? "Enabled" : "Disabled"}</span>
                      </div>
                    ) : (
                      <div className="td-control mt-4">
                        <input
                          aria-label={`${field.label} slider`}
                          className="td-range"
                          type="range"
                          min={field.min}
                          max={field.max}
                          step={field.step}
                          value={value}
                          onChange={(event) => {
                            setEditText((prev) => {
                              if (!(textKey in prev)) return prev;
                              const next = { ...prev };
                              delete next[textKey];
                              return next;
                            });
                            commitField(field, Number(event.target.value));
                          }}
                        />
                        <div className="td-num">
                          <input
                            aria-label={`${field.label} value`}
                            className={`input t-num${typedInvalid ? " input-error" : ""}`}
                            aria-invalid={typedInvalid || undefined}
                            type="number"
                            min={field.min}
                            max={field.max}
                            step={field.step}
                            value={text ?? fmt(value)}
                            onChange={(event) => {
                              const raw = event.target.value;
                              setEditText((prev) => ({ ...prev, [textKey]: raw }));
                              const parsed = Number(raw);
                              if (raw !== "" && Number.isFinite(parsed)) commitField(field, parsed);
                            }}
                            onBlur={() => setEditText((prev) => {
                              const next = { ...prev };
                              delete next[textKey];
                              return next;
                            })}
                          />
                          <span className="td-unit">{field.unit}</span>
                        </div>
                      </div>
                    )}
                    <span className="t-micro t-num">
                      {field.valueType === "boolean"
                        ? `Default ${field.default[transport] ? "On" : "Off"} · Recommended ${field.recommended[transport] ? "On" : "Off"}`
                        : `Range ${fmt(field.min)}–${fmt(field.max)} ${field.unit} · Default ${fmt(field.default[transport])} · Recommended ${fmt(field.recommended[transport])}`}
                    </span>
                    {typedInvalid && (
                      <span className="field-error">
                        <Icon name="alert" size={12} />
                        Must be between {fmt(field.min)} and {fmt(field.max)} — out-of-range values are clamped.
                      </span>
                    )}
                    {risk && !typedInvalid && (
                      <span className="td-risk">
                        <Icon name="alert" size={12} style={{ flexShrink: 0, marginTop: 2 }} />
                        {risk}
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          </section>
        );
      })}

      {serverErrors.length > 0 && (
        <Callout tone="critical" title="Check these values">
          {serverErrors.map((error) => <div key={error}>{error}</div>)}
        </Callout>
      )}

      {/* ── Sticky save bar ── */}
      <div className="td-savebar">
        <div className="row gap-8 grow wrap">
          {dirty ? (
            <span className="chip chip-warning"><Icon name="edit" size={12} /> Unsaved changes</span>
          ) : (
            <span className="chip chip-good"><Icon name="check-circle" size={12} /> All changes saved</span>
          )}
          {localErrors.length > 0 && (
            <span className="chip chip-critical">
              <Icon name="alert" size={12} />
              {localErrors.length} validation {localErrors.length === 1 ? "issue" : "issues"}
            </span>
          )}
          <span className="t-micro">Saved settings apply to new voice sessions.</span>
        </div>
        <div className="row gap-8 wrap" style={{ justifyContent: "flex-end" }}>
          {dirty && (
            <Button variant="ghost" onClick={discard} disabled={saving}>Discard</Button>
          )}
          <Button
            icon="sparkles"
            onClick={() => chooseMode("recommended")}
            disabled={saving}
            title="Balanced production profile tuned separately for browser and telephony"
          >
            Use Recommended Settings
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={saving || (draft.mode === "system_default" && Object.keys(draft.overrides).length === 0)}
            title="Remove every override and return to system defaults"
            onClick={() => setConfirmResetAll(true)}
          >
            Reset All to Default
          </Button>
          <Button
            variant="primary"
            icon="check"
            busy={saving}
            disabled={saving || !dirty || localErrors.length > 0}
            onClick={() => void save()}
          >
            Save Changes
          </Button>
        </div>
      </div>

      <ConfirmModal
        open={confirmResetAll}
        onClose={() => setConfirmResetAll(false)}
        onConfirm={resetAll}
        danger
        title="Reset all turn detection settings?"
        body={"Every override on both transports will be removed and the platform's system defaults will apply. This takes effect when you save."}
        confirmLabel="Reset All"
      />

      {/* ── Copy fallback: shown only when the clipboard is unavailable ── */}
      <Modal
        open={exportFallback !== null}
        onClose={() => setExportFallback(null)}
        title="Copy Configuration"
        sub="Clipboard access was blocked by the browser — copy the JSON below manually."
        footer={<Button variant="ghost" onClick={() => setExportFallback(null)}>Close</Button>}
      >
        <textarea
          className="input"
          aria-label="Exported configuration JSON"
          readOnly
          rows={12}
          style={MONO_TEXT}
          value={exportFallback ?? ""}
          onFocus={(event) => event.currentTarget.select()}
        />
      </Modal>

      {/* ── Import: paste → validate → preview → apply ── */}
      <Modal
        open={importOpen}
        onClose={() => !applying && setImportOpen(false)}
        title="Import Configuration"
        sub="Paste a Turn Detection configuration copied from any bot in any workspace."
        wide
        footer={
          <>
            <Button variant="ghost" onClick={() => setImportOpen(false)} disabled={applying}>Cancel</Button>
            <Button
              variant="primary"
              icon="check"
              busy={applying}
              disabled={!previewDocument || applying}
              title={previewDocument ? "Save the previewed configuration for this workspace" : "Validate the pasted JSON first"}
              onClick={() => void applyImport()}
            >
              Apply Configuration
            </Button>
          </>
        }
      >
        <div className="col gap-12">
          <p className="t-sub" style={{ margin: 0 }}>
            Turn detection is tenant-wide, so applying updates every bot in this workspace.
            Nothing is saved until you click Apply Configuration, and an invalid document is
            never applied — current settings stay unchanged.
          </p>
          {dirty && (
            <Callout tone="warning" title="Unsaved changes on this page">
              Applying an imported configuration replaces the unsaved edits on this page.
            </Callout>
          )}
          <textarea
            className="input"
            aria-label="Configuration JSON"
            rows={9}
            style={MONO_TEXT}
            placeholder={`{\n  "kind": "echosphere.turn-detection",\n  "schemaVersion": ${config.schemaVersion},\n  "mode": "custom",\n  "overrides": { … }\n}`}
            value={importText}
            onChange={(event) => {
              setImportText(event.target.value);
              setImportResult(null);
              setApplyError(null);
            }}
          />
          <div className="row gap-8 wrap">
            <Button
              icon="search"
              disabled={!importText.trim() || applying}
              onClick={() => setImportResult(parseTurnDetectionImport(config, importText))}
            >
              Validate &amp; Preview
            </Button>
            {previewDocument && (
              <span className="chip chip-good"><Icon name="check-circle" size={12} /> Valid configuration</span>
            )}
          </div>
          {importResult && importResult.errors.length > 0 && (
            <Callout
              tone="critical"
              title={`Configuration is invalid — ${importResult.errors.length} ${importResult.errors.length === 1 ? "issue" : "issues"}`}
            >
              {importResult.errors.map((error) => <div key={error}>{error}</div>)}
            </Callout>
          )}
          {importResult?.warnings.map((warning) => (
            <Callout key={warning} tone="warning">{warning}</Callout>
          ))}
          {previewDocument && (
            <section className="card">
              <div className="card-header">
                <div className="col gap-2">
                  <span className="card-title">Preview</span>
                  <span className="t-micro">Effective values after applying, compared with this workspace's current saved settings.</span>
                </div>
                <span className={previewDocument.mode === "system_default" ? "chip chip-neutral" : previewDocument.mode === "recommended" ? "chip chip-info" : "chip chip-brand"}>
                  <Icon name={MODE_ICONS[previewDocument.mode]} size={12} />
                  {previewMode?.label ?? previewDocument.mode}
                </span>
              </div>
              <div className="col gap-12" style={{ padding: 16 }}>
                {previewDocument.mode !== config.mode && (
                  <span className="t-sub">
                    Mode changes from <strong>{savedMode?.label ?? config.mode}</strong> to{" "}
                    <strong>{previewMode?.label ?? previewDocument.mode}</strong>.
                  </span>
                )}
                {previewChangeCount === 0 ? (
                  <span className="t-sub">All effective values match the current configuration.</span>
                ) : previewChanges.map(({ transport: item, rows }) => (
                  <div className="col gap-6" key={item.id}>
                    <span className="t-strong row gap-6" style={{ fontSize: 13 }}>
                      <Icon name={TRANSPORT_ICONS[item.id]} size={14} />
                      {item.label}
                      <span className="t-micro">
                        {rows.length ? `${rows.length} ${rows.length === 1 ? "value" : "values"} will change` : "no changes"}
                      </span>
                    </span>
                    {rows.map(({ field, current, imported }) => (
                      <div className="row-between gap-8" key={`${field.group}.${field.key}`} style={{ fontSize: 12.5 }}>
                        <span>{field.label}</span>
                        <span className="t-num">
                          {fmtFieldValue(field, current)} → <strong>{fmtFieldValue(field, imported)}</strong>
                          {field.valueType !== "boolean" && ` ${field.unit}`}
                        </span>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            </section>
          )}
          {applyError && (
            <Callout tone="critical" title="Could not apply configuration">
              {applyError} Existing settings were left unchanged.
            </Callout>
          )}
        </div>
      </Modal>
    </div>
  );
}
