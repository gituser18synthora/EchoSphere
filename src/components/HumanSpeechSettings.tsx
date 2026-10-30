import type {
  AmbiencePresetOption,
  HumanSpeechEffectiveSettings,
  HumanSpeechSettingKey,
  HumanSpeechSettingSource,
  HumanSpeechSettings,
  HumanSpeechSources,
} from "@/types/domain";
import { Button, Field, Toggle } from "@/components/ui";

type Scope = "tenant" | "bot";

interface Props {
  scope: Scope;
  override: HumanSpeechSettings;
  inherited: HumanSpeechEffectiveSettings;
  inheritedSources: HumanSpeechSources;
  onChange: (next: HumanSpeechSettings) => void;
  disabled?: boolean;
  collapseAdvanced?: boolean;
  /** Background sounds from the backend registry (only productionEnabled ones are offered). */
  ambiencePresets?: AmbiencePresetOption[];
}

interface BoolField { key: HumanSpeechSettingKey; label: string; help: string }
interface NumberField {
  key: HumanSpeechSettingKey;
  label: string;
  min: number;
  max: number;
  step: number;
  help: string;
}

interface ChoiceOption { value: string; label: string; description: string; selectable: boolean }
/** A select shown inline in its section (never under Advanced tuning). Its
 *  options come from a backend catalog, so the registry alone decides what
 *  is offered. */
interface ChoiceField {
  key: HumanSpeechSettingKey;
  label: string;
  help: string;
  catalog: keyof Catalogs;
  fallback: string;
}
/** Backend catalogs that choice fields draw their options from. */
interface Catalogs { ambiencePresets?: AmbiencePresetOption[] }

const catalogOptions = (field: ChoiceField, catalogs: Catalogs): ChoiceOption[] =>
  (catalogs[field.catalog] ?? []).map((preset) => ({
    value: preset.id,
    label: preset.label,
    description: preset.description,
    selectable: preset.productionEnabled,
  }));
/** A 0–N slider shown inline in its section (never under Advanced tuning). */
interface RangeField {
  key: HumanSpeechSettingKey;
  label: string;
  help: string;
  min: number;
  max: number;
  step: number;
  fallback: number;
  format: (value: number) => string;
}

/** One section of the editor. A section with a `master` key is a feature
 *  family (Breathing, Filler words, Call environment): its child controls
 *  only take effect while the master is on, and families never imply each
 *  other. */
interface Group {
  id: string;
  title: string;
  description: string;
  master?: BoolField;
  bools: BoolField[];
  numbers: NumberField[];
  choices?: ChoiceField[];
  ranges?: RangeField[];
}

export const DEFAULT_AMBIENCE_VOLUME = 50;

const LAYER: BoolField = {
  key: "enabled", label: "Human speech layer",
  help: "Master switch for every delivery-only natural conversation behavior below. Off = the bot speaks its replies and nothing else.",
};

export const GROUPS: Group[] = [
  {
    id: "breathing",
    title: "Breathing",
    description: "Subtle, voice-gender-matched breath sounds: nonverbal only. Works on its own — it never needs Filler words, and turning it off never touches them.",
    master: {
      key: "breathing", label: "Breathing",
      help: "All breath sounds: the breath before a reply and the rare breath inside a long reply. Independent of Filler words.",
    },
    bools: [
      { key: "latency_fillers", label: "Breath before the reply", help: "Play a short breath when a reply has not started speaking within the delay below. It stops the instant real speech starts and never holds the reply back. A ready acknowledgement (Filler words) replaces it at the same moment." },
      { key: "sentence_breaths", label: "Breath inside long replies", help: "In pause mode, allow a rare soft inhale before a long or verification sentence inside a reply. At most one per reply, never after every sentence." },
    ],
    numbers: [
      { key: "latency_filler_delay_ms", label: "Delay before the first gap sound (ms)", min: 500, max: 5000, step: 100, help: "Quiet time after the caller stops before the breath — or the acknowledgement, which uses the same moment — may play. Replies that start sooner get neither." },
      { key: "breath_gain_db", label: "Breathing volume (dB)", min: -24, max: 0, step: 0.5, help: "Lower values make breaths quieter. 0 keeps the original level. Spoken cues keep the bot's speech level." },
      { key: "sentence_breath_probability", label: "Sentence breath probability", min: 0, max: 1, step: 0.01, help: "Per eligible long or verification sentence (pause mode only)." },
    ],
  },
  {
    id: "filler-words",
    title: "Filler words",
    description: "Short spoken words that cover the wait for a reply, chosen per turn from what the caller just said — never a fixed sequence, never the same word on two turns in a row. Works on its own — it never needs Breathing, and turning it off never touches it.",
    master: {
      key: "filler_words", label: "Filler words",
      help: "All spoken fillers: the acknowledgement after the caller stops, the thinking and wait cues on long waits, the beat after a question and lookup prefaces. Independent of Breathing.",
    },
    bools: [
      { key: "acknowledgements", label: "Acknowledgements", help: "One short acknowledgement right after the caller stops (at the same delay as the breath), matched to what they said: \"जी…\" / \"अच्छा…\" / \"Hmm…\" while they explain or report a problem, \"ठीक है…\" only when they answered the bot's question. Never on two turns in a row." },
      { key: "latency_filler_ladder", label: "Thinking cues on long waits", help: "When the reply still has not started, a short cue in the bot's own voice (\"Hmm…\", \"जी…\", …) chosen for the context, then a spoken \"एक सेकंड…\" on a very long wait. Rendered once per voice. The spoken cue is withheld on critical or sensitive turns." },
      { key: "adaptive_latency_cues", label: "Adaptive thinking cues", help: "With thinking cues on, choose one contextual cue 1.5–2.5 seconds after the caller stops instead of a fixed cue time. Fast replies skip it; a started cue finishes with a 300 ms gap before the answer; caller interruptions stop it at once." },
      { key: "thinking_fillers", label: "Thinking fillers", help: "Allow a beat of thought (\"Hmm…\") as the acknowledgement when the caller asked a question, while the answer is being worked out." },
    ],
    numbers: [
      { key: "acknowledgement_probability", label: "Acknowledgement probability", min: 0, max: 1, step: 0.01, help: "Per eligible turn; halved for sensitive turns (complaints, refusals, dictated numbers)." },
      { key: "latency_cue_probability", label: "Long-wait cue probability", min: 0, max: 1, step: 0.01, help: "Chance that a long wait gets a voiced cue at all; the rest stay quiet (or a breath, if Breathing is on)." },
      { key: "latency_filler_hmm_ms", label: "Thinking cue at (ms)", min: 2000, max: 8000, step: 100, help: "Used when Adaptive thinking cues is off. Time after the caller stops before a voiced cue may play, when the reply still has not started." },
      { key: "latency_filler_spoken_ms", label: "Spoken wait cue at (ms)", min: 3000, max: 12000, step: 100, help: "Time after the caller stops before the spoken \"एक सेकंड…\" cue may play. Never on critical or sensitive turns." },
      { key: "thinking_filler_probability", label: "Thinking filler probability", min: 0, max: 1, step: 0.01, help: "Per eligible turn." },
      { key: "tool_ack_probability", label: "Tool lookup acknowledgement probability", min: 0, max: 1, step: 0.01, help: "Only safe, unambiguous lookup prefaces (\"ek minute, main check karta hoon…\") are eligible." },
    ],
  },
  {
    id: "backchannels",
    title: "While the caller speaks",
    description: "Sparse, non-semantic murmurs while the caller demonstrably still holds the floor.",
    bools: [
      { key: "backchannels", label: "Backchannels", help: "Allow sparse, non-semantic acknowledgements while a caller is demonstrably still speaking." },
    ],
    numbers: [
      { key: "backchannel_probability", label: "Backchannel probability", min: 0, max: 1, step: 0.01, help: "Per long-turn opportunity after safety gates." },
      { key: "min_long_turn_for_backchannel_ms", label: "Minimum long-turn duration (ms)", min: 1000, max: 60000, step: 500, help: "Caller must hold the floor at least this long." },
      { key: "min_gap_between_backchannels_ms", label: "Minimum backchannel gap (ms)", min: 2000, max: 120000, step: 500, help: "Cooldown between backchannel opportunities." },
      { key: "max_backchannels_per_call", label: "Maximum backchannels per call", min: 0, max: 20, step: 1, help: "Hard per-call cap." },
    ],
  },
  {
    id: "delivery",
    title: "Delivery",
    description: "How each sentence of a reply is paced and voiced.",
    bools: [
      { key: "prosody_variation", label: "Prosody variation", help: "Use safe provider-supported delivery variation with pause fallback." },
      { key: "micro_pauses", label: "Micro pauses", help: "Vary configured phrase gaps without adding blocking response delays." },
      { key: "gender_agreement", label: "Gender agreement", help: "Adapt authored first-person phrases to the active catalog voice identity." },
      { key: "self_correction", label: "Self-correction", help: "Enable rare direct-response correction. Streaming responses remain unchanged for safety." },
    ],
    numbers: [
      { key: "micro_pause_probability", label: "Micro-pause probability", min: 0, max: 1, step: 0.01, help: "Per non-critical sentence boundary." },
      { key: "self_correction_probability", label: "Self-correction probability", min: 0, max: 1, step: 0.001, help: "Kept extremely low and used only when self-correction is explicitly enabled." },
    ],
  },
  {
    id: "ambience",
    title: "Call environment",
    description: "What the caller hears around the bot's voice.",
    master: {
      key: "background_ambience", label: "Background ambience",
      help: "A quiet room sound under the whole call, including the pauses between turns, so the call sounds like it comes from a live environment. Mixed only into what the caller hears (never into speech recognition) and works with every voice provider. Off by default.",
    },
    bools: [],
    numbers: [],
    choices: [
      {
        key: "background_ambience_preset", label: "Background sound", fallback: "office",
        help: "Which room the caller hears.",
        catalog: "ambiencePresets",
      },
    ],
    ranges: [
      {
        key: "background_ambience_volume", label: "Background volume",
        min: 0, max: 100, step: 1, fallback: DEFAULT_AMBIENCE_VOLUME,
        help: "0 mutes the room; 50 is the standard level; 100 is the loudest allowed — always well below the bot's voice.",
        format: (value) => (value === 0 ? "Muted" : String(value)),
      },
    ],
  },
];

const BOOL_FIELDS: BoolField[] = [
  LAYER,
  ...GROUPS.flatMap((group) => [...(group.master ? [group.master] : []), ...group.bools]),
];
const NUMBER_FIELDS: NumberField[] = GROUPS.flatMap((group) => group.numbers);
const CHOICE_FIELDS: ChoiceField[] = GROUPS.flatMap((group) => group.choices ?? []);
const RANGE_FIELDS: RangeField[] = GROUPS.flatMap((group) => group.ranges ?? []);

export function validateHumanSpeechOverrides(
  override: HumanSpeechSettings,
  catalogs: Catalogs = {},
): string[] {
  const errors: string[] = [];
  for (const field of RANGE_FIELDS) {
    if (!hasOwn(override, field.key)) continue;
    const value = override[field.key];
    if (typeof value !== "number" || !Number.isInteger(value) || value < field.min || value > field.max) {
      errors.push(`${field.label} must be a whole number between ${field.min} and ${field.max}.`);
    }
  }
  for (const field of CHOICE_FIELDS) {
    if (!hasOwn(override, field.key)) continue;
    const options = catalogOptions(field, catalogs);
    if (!options.length) continue;   // no catalog (older API): the server validates
    // Any listed id is valid here: a withdrawn preset saved earlier may stay
    // (it cannot be picked again, and the server refuses it as a new choice).
    if (!options.some((option) => option.value === override[field.key])) {
      const offered = options.filter((option) => option.selectable).map((option) => option.label);
      errors.push(`${field.label} must be one of ${offered.join(", ")}.`);
    }
  }
  for (const field of NUMBER_FIELDS) {
    if (!hasOwn(override, field.key)) continue;
    const value = override[field.key];
    if (
      typeof value !== "number"
      || !Number.isFinite(value)
      || value < field.min
      || value > field.max
      // Whole-number steps are integer fields on the backend as well.
      || (Number.isInteger(field.step) && !Number.isInteger(value))
    ) {
      errors.push(`${field.label} must be between ${field.min} and ${field.max}.`);
    }
  }
  return errors;
}

const hasOwn = (value: HumanSpeechSettings, key: HumanSpeechSettingKey) =>
  Object.prototype.hasOwnProperty.call(value, key);

/** The switches that must be on for `key` to have any effect: the layer
 *  master, then its family master (Breathing / Filler words / Background
 *  ambience). */
export function gatesFor(key: HumanSpeechSettingKey): HumanSpeechSettingKey[] {
  if (key === "enabled") return [];
  const gates: HumanSpeechSettingKey[] = ["enabled"];
  for (const group of GROUPS) {
    if (!group.master || group.master.key === key) continue;
    const members = [
      ...group.bools, ...group.numbers, ...(group.choices ?? []), ...(group.ranges ?? []),
    ];
    if (members.some((field) => field.key === key)) {
      gates.push(group.master.key);
    }
  }
  return gates;
}

export function HumanSpeechSettingsEditor({
  scope,
  override,
  inherited,
  inheritedSources,
  onChange,
  disabled = false,
  collapseAdvanced = false,
  ambiencePresets,
}: Props) {
  const catalogs: Catalogs = { ambiencePresets };
  const sourceFor = (key: HumanSpeechSettingKey): HumanSpeechSettingSource =>
    hasOwn(override, key) ? scope : inheritedSources[key] ?? "platform";
  const valueFor = (key: HumanSpeechSettingKey): boolean | number => {
    if (key === "breath_gain_db") return override[key] ?? inherited[key] ?? 0;
    const own = override[key];
    const effective = own === undefined ? inherited[key] : own;
    // Keys a newer form knows but an older backend snapshot has not sent yet
    // default to their platform value (every switch defaults to on except
    // self-correction and background ambience).
    if (effective === undefined) {
      if (key === "self_correction" || key === "background_ambience") return false;
      return key === "latency_cue_probability" ? 0.7 : true;
    }
    return effective as boolean | number;
  };
  /** A choice's effective value; an unknown or missing one shows its fallback. */
  const choiceFor = (field: ChoiceField): string => {
    const own = override[field.key];
    const effective = own === undefined ? inherited[field.key] : own;
    const options = catalogOptions(field, catalogs);
    if (typeof effective !== "string") return field.fallback;
    return !options.length || options.some((option) => option.value === effective) ? effective : field.fallback;
  };
  /** A range's effective value, clamped to its bounds; missing = fallback. */
  const rangeFor = (field: RangeField): number => {
    const own = override[field.key];
    const effective = own === undefined ? inherited[field.key] : own;
    if (typeof effective !== "number" || !Number.isFinite(effective)) return field.fallback;
    return Math.min(field.max, Math.max(field.min, Math.round(effective)));
  };
  const setValue = (key: HumanSpeechSettingKey, value: boolean | number | string) =>
    onChange({ ...override, [key]: value });
  const clearValue = (key: HumanSpeechSettingKey) => {
    const next = { ...override };
    delete next[key];
    onChange(next);
  };
  const labelFor = (key: HumanSpeechSettingKey) => BOOL_FIELDS.find((field) => field.key === key)?.label ?? key;
  /** The first gate that is off for `key`, if any — why an "On" switch is inactive. */
  const inactiveBecause = (key: HumanSpeechSettingKey): string | null => {
    for (const gate of gatesFor(key)) {
      if (!valueFor(gate)) return gate === "enabled" ? "the Human speech layer is off" : `${labelFor(gate)} is off`;
    }
    return null;
  };

  const numericOverrideCount = NUMBER_FIELDS.filter((field) => hasOwn(override, field.key)).length;

  const numberControl = (field: NumberField) => {
    const overridden = hasOwn(override, field.key);
    const value = Number(valueFor(field.key));
    const source = sourceFor(field.key);
    return (
      <div key={field.key} className="card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
        <Field label={field.label} hint={`${field.help} Effective source: ${source}.`}>
          <div className="row gap-8">
            <input
              className="input t-num"
              aria-label={field.label}
              type="number"
              min={field.min}
              max={field.max}
              step={field.step}
              value={value}
              disabled={disabled}
              onChange={(event) => setValue(field.key, Number(event.target.value))}
            />
            {overridden && (
              <Button size="sm" variant="ghost" disabled={disabled} onClick={() => clearValue(field.key)}>
                Inherit
              </Button>
            )}
          </div>
        </Field>
      </div>
    );
  };

  const toggleCard = (field: BoolField, options: { master?: boolean } = {}) => {
    const overridden = hasOwn(override, field.key);
    const value = Boolean(valueFor(field.key));
    const source = sourceFor(field.key);
    const inactive = value ? inactiveBecause(field.key) : null;
    return (
      <div
        key={field.key}
        className="card-pad-sm col gap-6"
        data-testid={`human-speech-switch-${field.key}`}
        style={{
          border: options.master ? "1px solid var(--ink)" : "1px solid var(--hairline)",
          borderRadius: 10,
        }}
      >
        <div className="row-between gap-8">
          <div className="row gap-8">
            <Toggle
              checked={value}
              label={field.label}
              disabled={disabled}
              onChange={(next) => setValue(field.key, next)}
            />
            <span className="t-strong">{field.label}</span>
          </div>
          {overridden && (
            <Button size="sm" variant="ghost" disabled={disabled} onClick={() => clearValue(field.key)}>
              Inherit
            </Button>
          )}
        </div>
        <span className="t-micro">Effective: {value ? "On" : "Off"} · source: {source}</span>
        {inactive && (
          <span className="t-micro" data-testid={`human-speech-inactive-${field.key}`} style={{ color: "var(--status-warning, var(--ink-soft))" }}>
            Inactive while {inactive}.
          </span>
        )}
        <span className="field-hint">{field.help}</span>
      </div>
    );
  };

  const inactiveNote = (key: HumanSpeechSettingKey, reason: string | null) => reason && (
    <span className="t-micro" data-testid={`human-speech-inactive-${key}`} style={{ color: "var(--status-warning, var(--ink-soft))" }}>
      Inactive while {reason}.
    </span>
  );

  const choiceControl = (field: ChoiceField) => {
    const overridden = hasOwn(override, field.key);
    const value = choiceFor(field);
    const inactive = inactiveBecause(field.key);
    const options = catalogOptions(field, catalogs);
    const selected = options.find((option) => option.value === value);
    // Offered: the selectable options. A value saved before its option was
    // withdrawn stays visible (disabled) so it is shown truthfully and can be
    // changed, but never picked again.
    const shown = options.filter((option) => option.selectable || option.value === value);
    if (!selected) shown.push({ value, label: value, description: "", selectable: true });
    return (
      <div key={field.key} className="card-pad-sm col gap-6" data-testid={`human-speech-control-${field.key}`} style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
        <Field label={field.label} hint={`${selected?.description ?? ""} ${field.help}`.trim()}>
          <div className="row gap-8">
            <select
              className="select"
              aria-label={field.label}
              value={value}
              disabled={disabled || inactive !== null}
              onChange={(event) => setValue(field.key, event.target.value)}
            >
              {shown.map((option) => (
                <option key={option.value} value={option.value} disabled={!option.selectable}>
                  {option.selectable ? option.label : `${option.label} (not available)`}
                </option>
              ))}
            </select>
            {overridden && (
              <Button size="sm" variant="ghost" disabled={disabled} onClick={() => clearValue(field.key)}>
                Inherit
              </Button>
            )}
          </div>
        </Field>
        <span className="t-micro">Effective: {selected?.label ?? value} · source: {sourceFor(field.key)}</span>
        {selected && !selected.selectable && (
          <span className="t-micro" data-testid={`human-speech-unavailable-${field.key}`} style={{ color: "var(--status-warning, var(--ink-soft))" }}>
            {selected.label} is no longer offered. It stays saved until you choose another sound.
          </span>
        )}
        {inactiveNote(field.key, inactive)}
      </div>
    );
  };

  const rangeControl = (field: RangeField) => {
    const overridden = hasOwn(override, field.key);
    const value = rangeFor(field);
    const inactive = inactiveBecause(field.key);
    return (
      <div key={field.key} className="card-pad-sm col gap-6" data-testid={`human-speech-control-${field.key}`} style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
        <label className="col gap-6">
          <span className="row-between">
            <span className="field-label">{field.label}</span>
            <span className="t-num t-strong" style={{ fontSize: 12.5 }}>{field.format(value)}</span>
          </span>
          <input
            type="range"
            min={field.min}
            max={field.max}
            step={field.step}
            value={value}
            aria-label={field.label}
            disabled={disabled || inactive !== null}
            onChange={(event) => setValue(field.key, Math.round(Number(event.target.value)))}
            style={{ accentColor: "var(--brand-500)", width: "100%" }}
          />
        </label>
        <div className="row-between gap-8">
          <span className="t-micro">Effective: {field.format(value)} · source: {sourceFor(field.key)}</span>
          {overridden && (
            <Button size="sm" variant="ghost" disabled={disabled} onClick={() => clearValue(field.key)}>
              Inherit
            </Button>
          )}
        </div>
        {inactiveNote(field.key, inactive)}
        <span className="field-hint">{field.help}</span>
      </div>
    );
  };

  const groupSection = (group: Group) => (
    <section
      key={group.id}
      className="col gap-10"
      data-testid={`human-speech-group-${group.id}`}
      aria-labelledby={`human-speech-group-${group.id}-title`}
      style={{ border: "1px solid var(--hairline)", borderRadius: 12, padding: 12 }}
    >
      <div>
        <h3 id={`human-speech-group-${group.id}-title`} className="t-strong" style={{ margin: 0, fontSize: 15 }}>{group.title}</h3>
        <p className="t-micro" style={{ margin: "4px 0 0" }}>{group.description}</p>
      </div>
      {group.master && toggleCard(group.master, { master: true })}
      {group.bools.length > 0 && (
        <div className="grid grid-2" style={{ gap: 12 }}>
          {group.bools.map((field) => toggleCard(field))}
        </div>
      )}
      {((group.choices?.length ?? 0) > 0 || (group.ranges?.length ?? 0) > 0) && (
        <div className="grid grid-2" style={{ gap: 12 }}>
          {(group.choices ?? []).map(choiceControl)}
          {(group.ranges ?? []).map(rangeControl)}
        </div>
      )}
      {!collapseAdvanced && group.numbers.length > 0 && (
        <div className="grid grid-2" style={{ gap: 12 }}>
          {group.numbers.map(numberControl)}
        </div>
      )}
    </section>
  );

  return (
    <div className="col gap-14" data-testid={`human-speech-${scope}`}>
      <div className="row-between gap-12">
        <p className="t-sub" style={{ margin: 0 }}>
          {scope === "bot"
            ? "Bot values are sparse overrides; inherited tenant/platform values remain visible."
            : "Tenant values are sparse overrides of platform defaults and apply to every inheriting bot."}
        </p>
        <Button
          size="sm"
          variant="ghost"
          icon="undo"
          disabled={disabled || Object.keys(override).length === 0}
          onClick={() => onChange({})}
        >
          Clear all overrides
        </Button>
      </div>

      {toggleCard(LAYER, { master: true })}

      {GROUPS.map(groupSection)}

      {collapseAdvanced && (
        <details>
          <summary style={{ cursor: "pointer" }}>
            <span>Advanced tuning</span>
            {numericOverrideCount > 0 && (
              <span className="t-micro"> · {numericOverrideCount} {numericOverrideCount === 1 ? "override" : "overrides"}</span>
            )}
          </summary>
          <div className="col gap-12" style={{ marginTop: 12 }}>
            <p className="t-sub" style={{ margin: 0 }}>
              Adjust probabilities, timing, and per-call limits. Existing values stay active when this section is closed.
            </p>
            {GROUPS.filter((group) => group.numbers.length > 0).map((group) => (
              <div key={group.id} className="col gap-8" data-testid={`human-speech-advanced-${group.id}`}>
                <span className="t-micro t-strong">{group.title}</span>
                <div className="grid grid-2" style={{ gap: 12 }}>
                  {group.numbers.map(numberControl)}
                </div>
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}
