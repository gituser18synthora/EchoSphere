/* "Auto-detect language" — the one STT setting that decides whether a call can
   follow the caller between the bot's languages.

   Mirrors shared/providers/stt_language_policy.py:
   - an explicit true/false persisted in sttSettings wins;
   - otherwise the default is derived: more than one effective language →
     ON, a single language → OFF (recognizer pinned to the bot default).

   The control writes the key ONLY when the user touches it, so a fresh
   multilingual bot keeps following the derived default and an explicit OFF
   survives later language changes. "Use automatic" removes the key again. */

import type { ProviderSettings } from "@/types/domain";
import { Toggle } from "@/components/ui";

export const AUTO_DETECT_LANGUAGE_KEY = "auto_detect_language";

/** Settings keys owned by the platform's language policy — rendered by a
    dedicated control and never pre-filled from the provider schema. */
export const PLATFORM_OWNED_STT_KEYS: ReadonlySet<string> = new Set([AUTO_DETECT_LANGUAGE_KEY]);

export function explicitAutoDetect(settings: ProviderSettings | undefined): boolean | null {
  const v = settings?.[AUTO_DETECT_LANGUAGE_KEY];
  return typeof v === "boolean" ? v : null;
}

export function derivedAutoDetectDefault(languages: readonly string[] | undefined): boolean {
  const distinct = new Set((languages ?? []).map((l) => l.trim()).filter(Boolean));
  return distinct.size > 1;
}

export interface AutoDetectState {
  effective: boolean;
  source: "explicit" | "derived";
  derivedDefault: boolean;
  explicit: boolean | null;
}

export function autoDetectState(
  settings: ProviderSettings | undefined,
  languages: readonly string[] | undefined,
  /** Server-computed default (covers tenant inheritance when the bot has no languages). */
  serverDerivedDefault?: boolean,
): AutoDetectState {
  const derivedDefault = serverDerivedDefault ?? derivedAutoDetectDefault(languages);
  const explicit = explicitAutoDetect(settings);
  if (explicit === null) return { effective: derivedDefault, source: "derived", derivedDefault, explicit };
  return { effective: explicit, source: "explicit", derivedDefault, explicit };
}

export function SttAutoDetectControl({ settings, languages, serverDerivedDefault, sttLanguage, onChange, disabled }: {
  settings: ProviderSettings;
  languages: readonly string[];
  serverDerivedDefault?: boolean;
  /** Explicit STT language; when set the recognizer is pinned regardless. */
  sttLanguage?: string;
  onChange: (next: ProviderSettings) => void;
  disabled?: boolean;
}) {
  const state = autoDetectState(settings, languages, serverDerivedDefault);
  const count = new Set(languages.map((l) => l.trim()).filter(Boolean)).size;
  const pinnedByLanguage = Boolean(sttLanguage && sttLanguage.toLowerCase() !== "unknown");

  const setExplicit = (v: boolean) => onChange({ ...settings, [AUTO_DETECT_LANGUAGE_KEY]: v });
  const useAutomatic = () => {
    const next = { ...settings };
    delete next[AUTO_DETECT_LANGUAGE_KEY];
    onChange(next);
  };

  const sourceText = state.source === "explicit"
    ? `Set manually to ${state.explicit ? "on" : "off"}.`
    : `Automatic default: ${state.derivedDefault ? "on" : "off"} (${count === 1 ? "1 language" : `${count} languages`} configured).`;

  return (
    <div className="col gap-4" data-testid="stt-auto-detect">
      <div className="row-between">
        <div className="col gap-2">
          <span className="field-label">Auto-detect language</span>
          <span className="field-hint">
            {state.effective
              ? "The recognizer detects the spoken language on every utterance, so the bot can switch between its configured languages."
              : "Recognition is pinned to the bot's default language; the bot will not switch languages mid-call."}
          </span>
        </div>
        <Toggle
          checked={state.effective}
          onChange={setExplicit}
          label="Auto-detect language"
          disabled={disabled}
        />
      </div>
      <div className="row gap-8" style={{ alignItems: "center", flexWrap: "wrap" }}>
        <span className="t-sub" style={{ fontSize: 12 }} data-testid="stt-auto-detect-source">{sourceText}</span>
        {state.source === "explicit" && !disabled && (
          <button type="button" className="stt-auto-detect-reset" onClick={useAutomatic}
            aria-label="Use automatic language detection default">
            Use automatic
          </button>
        )}
      </div>
      {pinnedByLanguage && (
        <span className="t-sub" style={{ fontSize: 12 }} role="note">
          An explicit STT language is selected above, so recognition stays pinned to it. Choose
          &ldquo;Auto-detect&rdquo; there to let this setting take effect.
        </span>
      )}
      {state.effective && count <= 1 && (
        <span className="t-sub" style={{ fontSize: 12 }} role="note">
          This bot has only one language, so there is nothing to switch to — add languages in the
          Overview tab.
        </span>
      )}
    </div>
  );
}
