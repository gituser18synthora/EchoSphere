import type {
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
}

const BOOL_FIELDS: { key: HumanSpeechSettingKey; label: string; help: string }[] = [
  { key: "enabled", label: "Human speech layer", help: "Master switch for delivery-only natural conversation behavior." },
  { key: "thinking_fillers", label: "Thinking fillers", help: "Allow a beat of thought (\"हम्म…\") right after the caller asks a question, while the answer is being worked out." },
  { key: "acknowledgements", label: "Acknowledgements", help: "One short spoken acknowledgement (\"जी…\", \"ठीक है…\", \"एक सेकंड…\") right after the caller stops, matched to what they said; never on two turns in a row and never glued to the reply." },
  { key: "backchannels", label: "Backchannels", help: "Allow sparse, non-semantic acknowledgements while a caller is demonstrably still speaking." },
  { key: "prosody_variation", label: "Prosody variation", help: "Use safe provider-supported delivery variation with pause fallback." },
  { key: "gender_agreement", label: "Gender agreement", help: "Adapt authored first-person phrases to the active catalog voice identity." },
  { key: "micro_pauses", label: "Micro pauses", help: "Vary configured phrase gaps without adding blocking response delays." },
  { key: "self_correction", label: "Self-correction", help: "Enable rare direct-response correction. Streaming responses remain unchanged for safety." },
  { key: "latency_fillers", label: "Latency fillers", help: "Play a short breath matched to the voice's gender when a reply has not started speaking within the delay below; it stops the instant real speech starts and never holds the reply back." },
  { key: "sentence_breaths", label: "Sentence breaths", help: "In pause mode, allow a rare soft breath before a long or verification sentence inside a reply. At most one per reply, never after every sentence." },
  { key: "latency_filler_ladder", label: "Latency filler ladder", help: "On a long wait, follow the breath with a short \"हम्म…\" and then a spoken \"एक सेकंड…\" in the bot's own voice (rendered once per voice). The spoken cue is withheld on critical or sensitive turns." },
];

const NUMBER_FIELDS: {
  key: HumanSpeechSettingKey;
  label: string;
  min: number;
  max: number;
  step: number;
  help: string;
}[] = [
  { key: "thinking_filler_probability", label: "Thinking filler probability", min: 0, max: 1, step: 0.01, help: "Per eligible turn." },
  { key: "acknowledgement_probability", label: "Acknowledgement probability", min: 0, max: 1, step: 0.01, help: "Per eligible turn; halved for sensitive turns (complaints, refusals, dictated numbers)." },
  { key: "tool_ack_probability", label: "Tool lookup acknowledgement probability", min: 0, max: 1, step: 0.01, help: "Only safe, unambiguous lookup prefaces are eligible." },
  { key: "backchannel_probability", label: "Backchannel probability", min: 0, max: 1, step: 0.01, help: "Per long-turn opportunity after safety gates." },
  { key: "micro_pause_probability", label: "Micro-pause probability", min: 0, max: 1, step: 0.01, help: "Per non-critical sentence boundary." },
  { key: "self_correction_probability", label: "Self-correction probability", min: 0, max: 1, step: 0.001, help: "Kept extremely low and used only when self-correction is explicitly enabled." },
  { key: "sentence_breath_probability", label: "Sentence breath probability", min: 0, max: 1, step: 0.01, help: "Per eligible long or verification sentence (pause mode only)." },
  { key: "min_long_turn_for_backchannel_ms", label: "Minimum long-turn duration (ms)", min: 1000, max: 60000, step: 500, help: "Caller must hold the floor at least this long." },
  { key: "min_gap_between_backchannels_ms", label: "Minimum backchannel gap (ms)", min: 2000, max: 120000, step: 500, help: "Cooldown between backchannel opportunities." },
  { key: "max_backchannels_per_call", label: "Maximum backchannels per call", min: 0, max: 20, step: 1, help: "Hard per-call cap." },
  { key: "latency_filler_delay_ms", label: "Latency filler delay (ms)", min: 500, max: 5000, step: 100, help: "Quiet time after the caller stops speaking before a filler plays. Replies that start sooner never get one." },
  { key: "latency_filler_hmm_ms", label: "Ladder \"hmm\" cue at (ms)", min: 2000, max: 8000, step: 100, help: "Time after the caller stops before the voiced \"हम्म…\" cue may play, when the reply still has not started." },
  { key: "latency_filler_spoken_ms", label: "Ladder spoken cue at (ms)", min: 3000, max: 12000, step: 100, help: "Time after the caller stops before the spoken \"एक सेकंड…\" cue may play. Never on critical or sensitive turns." },
];

export function validateHumanSpeechOverrides(
  override: HumanSpeechSettings,
): string[] {
  const errors: string[] = [];
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

export function HumanSpeechSettingsEditor({
  scope,
  override,
  inherited,
  inheritedSources,
  onChange,
  disabled = false,
}: Props) {
  const sourceFor = (key: HumanSpeechSettingKey): HumanSpeechSettingSource =>
    hasOwn(override, key) ? scope : inheritedSources[key];
  const valueFor = (key: HumanSpeechSettingKey): boolean | number => {
    const own = override[key];
    return (own === undefined ? inherited[key] : own) as boolean | number;
  };
  const setValue = (key: HumanSpeechSettingKey, value: boolean | number) =>
    onChange({ ...override, [key]: value });
  const clearValue = (key: HumanSpeechSettingKey) => {
    const next = { ...override };
    delete next[key];
    onChange(next);
  };

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

      <div className="grid grid-2" style={{ gap: 12 }}>
        {BOOL_FIELDS.map((field) => {
          const overridden = hasOwn(override, field.key);
          const value = Boolean(valueFor(field.key));
          const source = sourceFor(field.key);
          return (
            <div key={field.key} className="card-pad-sm col gap-6" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
              <div className="row-between gap-8">
                <Toggle
                  checked={value}
                  label={field.label}
                  disabled={disabled}
                  onChange={(next) => setValue(field.key, next)}
                />
                {overridden && (
                  <Button size="sm" variant="ghost" disabled={disabled} onClick={() => clearValue(field.key)}>
                    Inherit
                  </Button>
                )}
              </div>
              <span className="t-micro">Effective: {value ? "On" : "Off"} · source: {source}</span>
              <span className="field-hint">{field.help}</span>
            </div>
          );
        })}
      </div>

      <div className="grid grid-2" style={{ gap: 12 }}>
        {NUMBER_FIELDS.map((field) => {
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
        })}
      </div>
    </div>
  );
}
