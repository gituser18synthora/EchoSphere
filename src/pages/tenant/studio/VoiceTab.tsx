/* Voice settings tab — provider-specific, database-driven configuration.
   Every provider / model / language / voice option shown here comes from the
   backend catalog APIs (/providers/*) — nothing is hardcoded. The backend
   re-validates the whole configuration on save; this UI mirrors those rules
   so problems surface before the PUT. */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type {
  AudioSettings, LanguageVoiceOverride, ModelLanguagesInfo, ProviderInfo,
  HumanSpeechSettings,
  ParamSpec, ProviderModelInfo, ProviderSettings, ProviderTestResult,
  TtsPreviewResult, VoiceBot, VoiceCapability, VoiceOption, VoiceSettings, VoiceTuning,
} from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import {
  generateTtsPreview, getModelLanguages, getProviderCatalog, getVoiceSettings,
  listLanguages, listProviderModels, listProviderVoices, saveVoiceSettings,
  testProviderConnection, validateVoiceConfig,
} from "@/services/api";
import type { ApiRequestError } from "@/services/http";
import {
  Button, Callout, CardSkeleton, ErrorState, Field, Modal, SearchableSelect, StatusChip,
  type SearchableSelectOption,
} from "@/components/ui";
import { Icon } from "@/components/Icon";
import { ParamFields, reconcileSettings, schemaDefaults } from "@/components/ProviderParams";
import { DictionaryField } from "@/components/PronunciationDictionaries";
import {
  HumanSpeechSettingsEditor,
  validateHumanSpeechOverrides,
} from "@/components/HumanSpeechSettings";
import { useApp } from "@/state/AppContext";

/* ---------- helpers ---------- */

const DEFAULT_SAMPLE_TEXT = "Hello! I'm your voice assistant. How can I help you today?";

/* Delivery tuning's Speaking speed is the single canonical speed control for
   a bot. The provider-specific duplicates (Sarvam `pace`, ElevenLabs `speed`)
   are hidden from this page and stripped from saved values — the runtime maps
   the canonical speed onto each engine. The admin voice catalog keeps the
   full schema. */
const DELIVERY_SPEED_PARAMS = ["pace", "speed"];

/* Platform turn timing is stored alongside STT provider settings, but is not
   a provider-native parameter. It therefore gets its own stable UI/contract
   instead of being added to every provider model's paramsSchema. */
const TURN_DETECTION_SCHEMA: Record<string, ParamSpec> = {
  barge_in_min_words: {
    type: "number", min: 0, max: 10, step: 1, default: 2,
    label: "Barge-in word threshold", help: "Transcribed words required before the caller can interrupt the bot mid-reply. Keeps background noise and chatter from cutting the bot off; 0 lets any detected voice activity interrupt instantly.",
  },
  user_speech_timeout: {
    type: "number", min: 0.2, max: 3, step: 0.05, default: 1.2,
    label: "User pause window", help: "Seconds of silence before the bot closes the caller's turn. Browser default: 1.2s; telephony default: 0.8s. Setting a value overrides both.",
  },
  finalize_grace: {
    type: "number", min: 0, max: 1.5, step: 0.05, default: 0.3,
    label: "Transcript finalization grace", help: "Extra seconds allowed for late STT transcript fragments before routing and response generation begin.",
  },
};

function turnDetectionValues(settings: ProviderSettings): ProviderSettings {
  const value = settings.turn_detection;
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function preserveTurnDetection(next: ProviderSettings, previous: ProviderSettings): ProviderSettings {
  const value = previous.turn_detection;
  return value && typeof value === "object" && !Array.isArray(value)
    ? { ...next, turn_detection: value }
    : next;
}

function stripDeliverySpeedParams<T>(record: Record<string, T> | undefined): Record<string, T> {
  return Object.fromEntries(
    Object.entries(record ?? {}).filter(([key]) => !DELIVERY_SPEED_PARAMS.includes(key)),
  );
}

/* Canonical speaking-speed bounds accepted by the voice-settings and preview
   APIs (shared/providers/tts/delivery.py SPEED_MIN/SPEED_MAX). A model's own
   range is intersected with these — bulbul:v2 documents pace down to 0.3, but
   0.5 is the slowest speed the platform can store. */
const CANONICAL_SPEED_MIN = 0.5;
const CANONICAL_SPEED_MAX = 2.0;

/** Slider bounds for the selected model, or null when it has no speed control.

    An ABSENT speedRange (a catalog response predating the field) falls back to
    the canonical range so speed keeps working; only an explicit null means the
    model documents no speed control at all. */
function effectiveSpeedRange(model: ProviderModelInfo | undefined): [number, number] | null {
  if (!model) return null;
  const range = model.speedRange;
  if (range === null) return null;
  if (range === undefined) return [CANONICAL_SPEED_MIN, CANONICAL_SPEED_MAX];
  return [Math.max(CANONICAL_SPEED_MIN, range[0]), Math.min(CANONICAL_SPEED_MAX, range[1])];
}

function voiceSupportsModel(v: VoiceOption, model: string): boolean {
  return !model || v.modelCodes.length === 0 || v.modelCodes.includes(model);
}

function voiceSupportsLanguage(v: VoiceOption, locale: string): boolean {
  return !locale || v.languages.length === 0 || v.languages.includes(locale);
}

function findVoice(voices: VoiceOption[] | undefined, id: string): VoiceOption | undefined {
  return voices?.find((v) => v.id === id || v.providerVoiceId === id);
}

/** Ensure the current value is always rendered, even when it fell out of the
    catalog — as an explicit unavailable entry that cannot be re-selected. */
function withCurrent(options: SearchableSelectOption[], value: string): SearchableSelectOption[] {
  if (!value || options.some((o) => o.value === value)) return options;
  return [
    { value, label: `${value} (unavailable)`, sub: "not in the catalog for this selection — pick a replacement", disabled: true },
    ...options,
  ];
}

/* ---------- engine state ---------- */

interface SttState { provider: string; model: string; language: string; settings: ProviderSettings }
interface LlmState { provider: string; model: string; settings: ProviderSettings }
interface TtsState { provider: string; model: string; voice: string; settings: ProviderSettings }
interface FallbackState { provider: string; model: string; voice: string }

/** What the preview modal hands back when the user applies a tuned draft. */
export interface PreviewDraft {
  provider: string; model: string; voice: string;
  settings: ProviderSettings;
  /** Bot-level Delivery tuning drafts (speed maps to Sarvam pace / ElevenLabs speed). */
  speed: number;
  pauseMs: number;
  empathy: number;
  energy: number;
}

interface PreviewContext {
  provider: string; model: string; voice: string; language: string; params?: ProviderSettings;
  tuning?: VoiceTuning;
  /* Staging callback: writes the tuned draft into page state. It never
     persists — the explicit "Save voice settings" action still does that.
     Engine + settings go to the launch target (default engine or language
     row); the delivery values are bot-level and stage identically from
     every launch point, so both preview buttons share ONE modal behavior. */
  onApply?: (draft: PreviewDraft) => void;
  /** The staged per-language override for a locale, exactly as a live call
      would resolve it — null means the locale inherits the default engine. */
  resolveOverride?: (locale: string) => LanguageVoiceOverride | null;
  /** The staged default engine — what an inheriting locale falls back to.
      Needed when the modal was launched from a per-language row. */
  defaultEngine?: { provider: string; model: string; voice: string; params?: ProviderSettings };
  /** The apply target runs inside the realtime streaming router (a
      per-language row): non-streaming models can be previewed but never
      applied, and are disabled in the model dropdown with the reason. */
  requiresStreaming?: boolean;
}

type TestState = Record<string, { busy: boolean; result: ProviderTestResult | null }>;

/* ============================================================ */

export default function VoiceTab({ bot }: { bot: VoiceBot }) {
  const { toast, hasPermission } = useApp();
  const canManage = hasPermission("manage_voices") || hasPermission("bots.manage");
  const noPermTitle = canManage ? undefined : "Requires the manage_voices permission";

  const settingsQ = useAsync(() => getVoiceSettings(bot.id), [bot.id]);
  const catalogQ = useAsync(() => getProviderCatalog(), []);
  /* Readable language names for the per-language section — locale codes stay
     the internal values. Disabled languages are included so a bot that still
     references one keeps its readable name (flagged in the row instead).
     A failed load silently falls back to the codes. */
  const languagesQ = useAsync(() => listLanguages(true).catch(() => []), []);

  const [tuning, setTuning] = useState<VoiceTuning>({ speed: 1, pauseMs: 350, empathy: 50, energy: 50 });
  const [stt, setStt] = useState<SttState>({ provider: "", model: "", language: "", settings: {} });
  const [llm, setLlm] = useState<LlmState>({ provider: "", model: "", settings: {} });
  const [tts, setTts] = useState<TtsState>({ provider: "", model: "", voice: "", settings: {} });
  const [fallback, setFallback] = useState<FallbackState>({ provider: "", model: "", voice: "" });
  const [langMap, setLangMap] = useState<Record<string, string | LanguageVoiceOverride>>({});
  const [audio, setAudio] = useState<AudioSettings>({
    browser: { codec: "linear16", sampleRate: 16000 },
    telephony: { codec: "mulaw", sampleRate: 8000 },
  });
  const [humanSpeech, setHumanSpeech] = useState<HumanSpeechSettings>({});
  const [saving, setSaving] = useState(false);
  const [validating, setValidating] = useState(false);
  const [saveErrors, setSaveErrors] = useState<string[]>([]);
  const [saveWarnings, setSaveWarnings] = useState<string[]>([]);
  const [warningsFromSave, setWarningsFromSave] = useState(false);
  const [tests, setTests] = useState<TestState>({});
  const [preview, setPreview] = useState<PreviewContext | null>(null);

  /* Model / voice caches, keyed by "<capability>:<provider>" and "<provider>".
     cacheTick bumps on every cache fill so memos over the refs recompute. */
  const modelsRef = useRef<Record<string, ProviderModelInfo[]>>({});
  const voicesRef = useRef<Record<string, VoiceOption[]>>({});
  const [cacheTick, setCacheTick] = useState(0);

  const ensureModels = useCallback(async (cap: VoiceCapability, provider: string): Promise<ProviderModelInfo[]> => {
    const key = `${cap}:${provider}`;
    const hit = modelsRef.current[key];
    if (hit) return hit;
    try {
      const models = await listProviderModels(cap, provider);
      modelsRef.current[key] = models;
      setCacheTick((t) => t + 1);
      return models;
    } catch (e) {
      toast(e instanceof Error ? e.message : `Could not load ${provider} models`, "error");
      return [];
    }
  }, [toast]);

  const ensureVoices = useCallback(async (provider: string): Promise<VoiceOption[]> => {
    const hit = voicesRef.current[provider];
    if (hit) return hit;
    try {
      const voices = await listProviderVoices(provider);
      voicesRef.current[provider] = voices;
      setCacheTick((t) => t + 1);
      return voices;
    } catch (e) {
      toast(e instanceof Error ? e.message : `Could not load ${provider} voices`, "error");
      return [];
    }
  }, [toast]);

  const modelsFor = (cap: VoiceCapability, provider: string): ProviderModelInfo[] =>
    modelsRef.current[`${cap}:${provider}`] ?? [];
  const modelInfo = (cap: VoiceCapability, provider: string, model: string): ProviderModelInfo | undefined =>
    modelsFor(cap, provider).find((m) => m.code === model);

  /* ---- initialize from persisted settings ---- */
  useEffect(() => {
    const s = settingsQ.data;
    if (!s) return;
    setTuning({ speed: s.speed, pauseMs: s.pauseMs, empathy: s.empathy, energy: s.energy });
    setStt({ provider: s.sttProvider ?? "", model: s.sttModel ?? "", language: s.sttLanguage ?? "", settings: s.sttSettings ?? {} });
    setLlm({ provider: s.llmProvider ?? "", model: s.llmModel ?? "", settings: s.llmSettings ?? {} });
    /* Legacy pace/speed duplicates are dropped so Delivery tuning stays the
       only speed control (they are also stripped server-side on save). */
    setTts({ provider: s.ttsProvider ?? "", model: s.ttsModel ?? "", voice: s.ttsVoice ?? "", settings: stripDeliverySpeedParams(s.ttsSettings ?? {}) });
    setFallback({ provider: s.fallbackProvider ?? "", model: s.fallbackModel ?? "", voice: s.fallbackVoice ?? "" });
    setLangMap(() => {
      /* Keep only entries for the bot's current languages plus the "default" locale key. */
      const map = s.languageVoiceMap ?? {};
      const next: Record<string, string | LanguageVoiceOverride> = {};
      for (const l of bot.languages) {
        const entry = map[l];
        if (entry !== undefined && entry !== "") next[l] = entry;
      }
      const def = map["default"];
      if (typeof def === "string" && def) next["default"] = def;
      return next;
    });
    setAudio({
      browser: { codec: "linear16", sampleRate: s.audioSettings?.browser?.sampleRate ?? 16000 },
      telephony: { codec: s.audioSettings?.telephony?.codec ?? "mulaw", sampleRate: 8000 },
    });
    setHumanSpeech(s.humanSpeech ?? {});
  }, [settingsQ.data, bot.languages]);

  /* ---- prefetch models/voices for everything currently selected ---- */
  useEffect(() => { if (stt.provider) void ensureModels("stt", stt.provider); }, [stt.provider, ensureModels]);
  useEffect(() => { if (llm.provider) void ensureModels("llm", llm.provider); }, [llm.provider, ensureModels]);
  useEffect(() => {
    if (tts.provider) { void ensureModels("tts", tts.provider); void ensureVoices(tts.provider); }
  }, [tts.provider, ensureModels, ensureVoices]);
  useEffect(() => {
    if (fallback.provider) { void ensureModels("tts", fallback.provider); void ensureVoices(fallback.provider); }
  }, [fallback.provider, ensureModels, ensureVoices]);
  useEffect(() => {
    for (const entry of Object.values(langMap)) {
      if (entry && typeof entry === "object" && entry.provider) {
        void ensureModels("tts", entry.provider);
        void ensureVoices(entry.provider);
      }
    }
  }, [langMap, ensureModels, ensureVoices]);

  /* Platform languages supported by the selected STT model. */
  const sttLangQ = useAsync<ModelLanguagesInfo | null>(
    () => (stt.provider && stt.model ? getModelLanguages("stt", stt.provider, stt.model) : Promise.resolve(null)),
    [stt.provider, stt.model],
  );

  const sttProviders = catalogQ.data?.stt ?? [];
  const ttsProviders = catalogQ.data?.tts ?? [];
  const llmProviders = catalogQ.data?.llm ?? [];
  const defaultLocale = typeof langMap["default"] === "string" ? (langMap["default"] as string) : "";

  /* ---- revalidation: invalid selections are only removed on "Apply changes" ---- */

  const sttIssues = useMemo(() => {
    const d = sttLangQ.data;
    if (!d || !stt.language || d.languageAgnostic) return [];
    return d.languages.some((l) => l.code === stt.language)
      ? []
      : [`Language "${stt.language}" is not supported by ${stt.provider}/${stt.model} — it will be reset to auto/unset.`];
  }, [sttLangQ.data, stt.language, stt.provider, stt.model]);

  const ttsIssues = useMemo(() => {
    if (!tts.provider || !tts.voice) return [];
    const voices = voicesRef.current[tts.provider];
    if (!voices) return [];
    const v = findVoice(voices, tts.voice);
    if (!v) return [`Voice "${tts.voice}" is not in the ${tts.provider} catalog — it will be removed.`];
    const issues: string[] = [];
    if (!voiceSupportsModel(v, tts.model)) issues.push(`Voice "${v.name}" does not support model "${tts.model}" — it will be removed.`);
    else if (defaultLocale && !voiceSupportsLanguage(v, defaultLocale)) issues.push(`Voice "${v.name}" does not support the default language "${defaultLocale}" — it will be removed.`);
    return issues;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tts.provider, tts.model, tts.voice, defaultLocale, cacheTick]);

  const fallbackIssues = useMemo(() => {
    if (!fallback.provider || !fallback.voice) return [];
    const voices = voicesRef.current[fallback.provider];
    if (!voices) return [];
    const v = findVoice(voices, fallback.voice);
    if (!v) return [`Voice "${fallback.voice}" is not in the ${fallback.provider} catalog — it will be removed.`];
    if (!voiceSupportsModel(v, fallback.model)) return [`Voice "${v.name}" does not support model "${fallback.model}" — it will be removed.`];
    return [];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fallback, cacheTick]);

  const mappingIssues = useMemo(() => {
    const issues: { locale: string; message: string }[] = [];
    for (const locale of bot.languages) {
      const entry = langMap[locale];
      if (!entry || typeof entry === "string" || !entry.provider || !entry.voice) continue;
      const voices = voicesRef.current[entry.provider];
      if (!voices) continue;
      const v = findVoice(voices, entry.voice);
      if (!v) issues.push({ locale, message: `${locale}: voice "${entry.voice}" is not in the ${entry.provider} catalog — it will be removed.` });
      else if (!voiceSupportsModel(v, entry.model)) issues.push({ locale, message: `${locale}: voice "${v.name}" does not support model "${entry.model}" — it will be removed.` });
      else if (!voiceSupportsLanguage(v, locale)) issues.push({ locale, message: `${locale}: voice "${v.name}" does not support this language — it will be removed.` });
    }
    return issues;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [langMap, bot.languages, cacheTick]);

  const langCatalog = useMemo(() => {
    const map: Record<string, { name: string; enabled: boolean }> = {};
    for (const l of languagesQ.data ?? []) map[l.code] = { name: l.name, enabled: l.enabled };
    return map;
  }, [languagesQ.data]);

  if (settingsQ.error) return <ErrorState message={settingsQ.error} onRetry={settingsQ.reload} />;
  if (catalogQ.error) return <ErrorState message={catalogQ.error} onRetry={catalogQ.reload} />;
  if (settingsQ.loading || catalogQ.loading) {
    return <div className="grid grid-2">{Array.from({ length: 6 }).map((_, i) => <CardSkeleton key={i} rows={4} />)}</div>;
  }

  /* ---- change handlers (provider change resets model-dependent fields) ---- */

  const defaultModelOf = async (cap: VoiceCapability, provider: string) => {
    const models = await ensureModels(cap, provider);
    return models.find((m) => m.isDefault) ?? models[0];
  };

  const changeSttProvider = async (provider: string) => {
    setStt((s) => ({ provider, model: "", language: "", settings: preserveTurnDetection({}, s.settings) }));
    if (!provider) return;
    const def = await defaultModelOf("stt", provider);
    setStt((s) => (s.provider === provider && !s.model
      ? { ...s, model: def?.code ?? "", settings: preserveTurnDetection(schemaDefaults(def?.paramsSchema), s.settings) }
      : s));
  };
  const changeSttModel = (model: string) => {
    const schema = modelInfo("stt", stt.provider, model)?.paramsSchema;
    setStt((s) => ({
      ...s,
      model,
      settings: preserveTurnDetection(reconcileSettings(schema, s.settings), s.settings),
    }));
  };

  const changeLlmProvider = async (provider: string) => {
    setLlm({ provider, model: "", settings: {} });
    if (!provider) return;
    const def = await defaultModelOf("llm", provider);
    setLlm((s) => (s.provider === provider && !s.model
      ? { ...s, model: def?.code ?? "", settings: schemaDefaults(def?.paramsSchema) }
      : s));
  };
  const changeLlmModel = (model: string) => {
    const schema = modelInfo("llm", llm.provider, model)?.paramsSchema;
    setLlm((s) => ({ ...s, model, settings: reconcileSettings(schema, s.settings) }));
  };

  const changeTtsProvider = async (provider: string) => {
    setTts({ provider, model: "", voice: "", settings: {} });
    if (!provider) return;
    void ensureVoices(provider);
    const def = await defaultModelOf("tts", provider);
    setTts((s) => (s.provider === provider && !s.model
      ? { ...s, model: def?.code ?? "", settings: schemaDefaults(stripDeliverySpeedParams(def?.paramsSchema)) }
      : s));
  };
  const changeTtsModel = (model: string) => {
    const schema = stripDeliverySpeedParams(modelInfo("tts", tts.provider, model)?.paramsSchema);
    setTts((s) => ({ ...s, model, settings: reconcileSettings(schema, s.settings) }));
  };

  const changeFallbackProvider = async (provider: string) => {
    setFallback({ provider, model: "", voice: "" });
    if (!provider) return;
    void ensureVoices(provider);
    const def = await defaultModelOf("tts", provider);
    setFallback((f) => (f.provider === provider && !f.model ? { ...f, model: def?.code ?? "" } : f));
  };

  const setRowProvider = async (locale: string, provider: string) => {
    if (!provider) {
      setLangMap((m) => { const next = { ...m }; delete next[locale]; return next; });
      return;
    }
    setLangMap((m) => ({ ...m, [locale]: { provider, model: "", voice: "" } }));
    void ensureVoices(provider);
    const def = await defaultModelOf("tts", provider);
    setLangMap((m) => {
      const cur = m[locale];
      if (!cur || typeof cur === "string" || cur.provider !== provider || cur.model) return m;
      return { ...m, [locale]: { ...cur, model: def?.code ?? "" } };
    });
  };
  const setRowField = (locale: string, patch: Partial<LanguageVoiceOverride>) =>
    setLangMap((m) => {
      const cur = m[locale];
      if (!cur || typeof cur === "string") return m;
      return { ...m, [locale]: { ...cur, ...patch } };
    });

  /* A model change keeps the voice only when it verifiably supports the new
     model and this language — an incompatible pair is never left staged. */
  const setRowModel = (locale: string, model: string) =>
    setLangMap((m) => {
      const cur = m[locale];
      if (!cur || typeof cur === "string") return m;
      const v = cur.voice ? findVoice(voicesRef.current[cur.provider], cur.voice) : undefined;
      const keepVoice = Boolean(v && voiceSupportsModel(v, model) && voiceSupportsLanguage(v, locale));
      return { ...m, [locale]: { ...cur, model, voice: keepVoice ? cur.voice : "" } };
    });

  const clearRow = (locale: string) =>
    setLangMap((m) => { const next = { ...m }; delete next[locale]; return next; });

  const langLabel = (code: string) => langCatalog[code]?.name ?? code;

  /* The staged override a live call would resolve for a locale (mirrors the
     runtime language_map semantics). Incomplete rows and legacy voice-id
     entries fall back to the default engine, exactly like the runtime. */
  const resolveOverride = (locale: string): LanguageVoiceOverride | null => {
    const entry = langMap[locale];
    if (!entry || typeof entry === "string") return null;
    return entry.provider && entry.model && entry.voice ? entry : null;
  };

  /* Per-row status — shown as an icon+text chip (never color alone). */
  const rowStatus = (locale: string): { chip: string; label: string; message?: string } => {
    const entry = langMap[locale];
    if (!entry) return { chip: "available", label: "Inherits default" };
    if (typeof entry === "string") return { chip: "warning", label: "Legacy" };
    if (entry.provider && !ttsProviders.some((p) => p.code === entry.provider)) {
      return { chip: "error", label: "Unavailable", message: `Provider "${entry.provider}" is no longer available — select an active provider before saving.` };
    }
    const issue = mappingIssues.find((i) => i.locale === locale);
    if (issue) return { chip: "error", label: "Unavailable", message: issue.message };
    const models = modelsRef.current[`tts:${entry.provider}`];
    if (entry.model && models && !models.some((m) => m.code === entry.model)) {
      return { chip: "error", label: "Unavailable", message: `Model "${entry.model}" is no longer available for ${entry.provider} — select an active model before saving.` };
    }
    /* A model that exists but cannot stream in realtime (Eleven v3) can never
       serve a per-language override — live calls switch languages mid-call.
       The backend rejects it on save; surface the reason here first. */
    const rowModel = entry.model ? models?.find((m) => m.code === entry.model) : undefined;
    if (rowModel && !rowModel.streaming) {
      return {
        chip: "error", label: "Unavailable",
        message: `Model "${entry.model}" does not support realtime streaming, so it cannot serve per-language overrides — pick a streaming model (e.g. Eleven Flash v2.5).`,
      };
    }
    if (!entry.model || !entry.voice) return { chip: "warning", label: "Incomplete" };
    return { chip: "active", label: "Active" };
  };

  /* ---- voice option builders ---- */

  const voiceSelectOptions = (provider: string, model: string, locale?: string): SearchableSelectOption[] =>
    (voicesRef.current[provider] ?? [])
      .filter((v) => voiceSupportsModel(v, model) && (!locale || voiceSupportsLanguage(v, locale)))
      .map((v) => ({
        value: v.id,
        label: `${v.name}${v.source === "cloned" ? " · cloned" : ""}${v.isDefault ? " · default" : ""}${v.premium ? " · premium" : ""}`,
        sub: [v.gender, v.locale, v.status && v.status !== "active" ? v.status : null].filter(Boolean).join(" · "),
        disabled: v.status === "unavailable",
      }));

  /* ---- connection tests ---- */

  const runTest = async (key: string, body: { capability: VoiceCapability; provider: string; model?: string; voice?: string; language?: string }) => {
    setTests((t) => ({ ...t, [key]: { busy: true, result: null } }));
    try {
      const result = await testProviderConnection(body);
      setTests((t) => ({ ...t, [key]: { busy: false, result } }));
      if (result.ok) {
        toast(`${body.provider} ${body.capability.toUpperCase()} connection OK${result.latencyMs !== undefined ? ` — ${Math.round(result.latencyMs)} ms` : ""}`);
      } else {
        toast(result.message || "Connection test failed", "error");
      }
    } catch (e) {
      const message = e instanceof Error ? e.message : "Connection test failed";
      setTests((t) => ({ ...t, [key]: { busy: false, result: { ok: false, error: "error", message } } }));
      toast(message, "error");
    }
  };

  /* ---- validate / save ---- */

  const assembleConfig = () => ({
    sttProvider: stt.provider, sttModel: stt.model, sttLanguage: stt.language, sttSettings: stt.settings,
    llmProvider: llm.provider, llmModel: llm.model, llmSettings: llm.settings,
    ttsProvider: tts.provider, ttsModel: tts.model, ttsVoice: tts.voice, ttsSettings: tts.settings,
    languageVoiceMap: langMap,
    fallbackProvider: fallback.provider, fallbackModel: fallback.model, fallbackVoice: fallback.voice,
    audioSettings: audio,
    humanSpeech,
  });

  const validate = async () => {
    setValidating(true); setSaveErrors([]); setSaveWarnings([]); setWarningsFromSave(false);
    const localErrors = validateHumanSpeechOverrides(humanSpeech);
    if (localErrors.length) {
      setSaveErrors(localErrors);
      toast("Natural conversation settings are outside the allowed bounds", "error");
      setValidating(false);
      return;
    }
    try {
      const r = await validateVoiceConfig(bot.id, assembleConfig());
      setSaveErrors(r.errors);
      setSaveWarnings(r.warnings);
      if (r.valid) toast(r.warnings.length ? "Configuration is valid — with warnings" : "Configuration is valid");
      else toast("The catalog rejected this configuration", "error");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Validation failed", "error");
    } finally {
      setValidating(false);
    }
  };

  const save = async () => {
    setSaving(true); setSaveErrors([]); setSaveWarnings([]); setWarningsFromSave(false);
    const localErrors = validateHumanSpeechOverrides(humanSpeech);
    if (localErrors.length) {
      setSaveErrors(localErrors);
      toast("Natural conversation settings are outside the allowed bounds", "error");
      setSaving(false);
      return;
    }
    /* Empty strings (not nulls) clear overrides server-side; settings objects are sent whole. */
    const payload: Partial<VoiceSettings> = {
      voiceId: tts.voice || null,
      speed: tuning.speed, pauseMs: tuning.pauseMs, empathy: tuning.empathy, energy: tuning.energy,
      ...assembleConfig(),
    };
    try {
      const { warnings } = await saveVoiceSettings(bot.id, payload);
      if (warnings.length) {
        setSaveWarnings(warnings);
        setWarningsFromSave(true);
        toast(`Saved with warnings: ${warnings[0]}${warnings.length > 1 ? ` (+${warnings.length - 1} more)` : ""}`, "info");
      } else {
        toast("Voice settings saved to draft — publish to make them live");
      }
      settingsQ.reload();
    } catch (e) {
      const err = e as ApiRequestError;
      setSaveErrors(err.errors?.length ? err.errors : [err.message || "Could not save voice settings"]);
      toast("Could not save voice settings", "error");
    } finally {
      setSaving(false);
    }
  };

  /* ---- STT language options ---- */
  const sttLangOptions: SearchableSelectOption[] = [];
  if (sttLangQ.data?.supportsAutoDetect) {
    sttLangOptions.push({ value: "", label: "Auto-detect", sub: "the model detects the spoken language" });
  }
  for (const l of sttLangQ.data?.languages ?? []) {
    sttLangOptions.push({ value: l.code, label: l.nativeName ? `${l.name} · ${l.nativeName}` : l.name, sub: l.code });
  }

  const sttModels = modelsFor("stt", stt.provider);
  const llmModels = modelsFor("llm", llm.provider);
  const ttsModels = modelsFor("tts", tts.provider);
  /* Fallback synthesis happens inside the realtime streaming router — every
     active model is LISTED, but non-streaming ones (Eleven v3) render
     disabled with the reason; the backend rejects them too. */
  const fallbackModels = modelsFor("tts", fallback.provider);
  const sttSchema = modelInfo("stt", stt.provider, stt.model)?.paramsSchema;
  const llmSchema = modelInfo("llm", llm.provider, llm.model)?.paramsSchema;
  /* Provider pace/speed never renders here — Delivery tuning owns speed. */
  const ttsSchema = stripDeliverySpeedParams(modelInfo("tts", tts.provider, tts.model)?.paramsSchema);

  /* What an inheriting language actually uses — the primary TTS engine. */
  const primaryEngineSummary = tts.provider
    ? [
        ttsProviders.find((p) => p.code === tts.provider)?.name ?? tts.provider,
        modelInfo("tts", tts.provider, tts.model)?.displayName ?? tts.model,
        tts.voice ? (findVoice(voicesRef.current[tts.provider], tts.voice)?.name ?? tts.voice) : "",
      ].filter(Boolean).join(" · ")
    : "the platform default engine";

  return (
    <div className="col gap-16">
      <div className="voice-grid">
        {/* ── left column: engines ── */}
        <div className="col gap-16">
          {/* 1 — Speech-to-Text */}
          <SectionCard
            title="Speech-to-Text" sub="Transcribes what the caller says"
            actions={
              <TestControl
                state={tests.stt} disabled={!stt.provider || !canManage} title={noPermTitle}
                onTest={() => void runTest("stt", { capability: "stt", provider: stt.provider, model: stt.model || undefined, language: stt.language || undefined })}
              />
            }
          >
            <ProviderSelect
              label="Provider" capability="stt" providers={sttProviders}
              value={stt.provider} onChange={(v) => void changeSttProvider(v)}
            />
            <ModelSelect
              label="Model" ariaLabel="STT model" models={sttModels} value={stt.model}
              disabled={!stt.provider} onChange={changeSttModel}
            />
            {stt.provider && stt.model && (
              <Field
                label="Language" plain
                hint={sttLangQ.data?.languageAgnostic ? "This model is language-agnostic — a language hint is optional." : undefined}
              >
                <SearchableSelect
                  options={withCurrent(sttLangOptions, stt.language)}
                  value={stt.language}
                  onChange={(v) => setStt((s) => ({ ...s, language: v }))}
                  placeholder={sttLangQ.loading ? "Loading languages…" : "Auto / not set"}
                  searchPlaceholder="Search languages…"
                  ariaLabel="STT language"
                />
              </Field>
            )}
            <ParamFields
              key={`stt:${stt.provider}:${stt.model}`}
              schema={sttSchema} values={stt.settings}
              onChange={(next) => setStt((s) => ({ ...s, settings: next }))}
            />
            <details>
              <summary className="t-label" style={{ cursor: "pointer" }}>Turn response timing</summary>
              <div className="col gap-12" style={{ marginTop: 10 }}>
                <span className="field-hint">
                  Lower values respond faster; values that are too low can cut off callers who pause mid-sentence.
                </span>
                <ParamFields
                  schema={TURN_DETECTION_SCHEMA}
                  values={turnDetectionValues(stt.settings)}
                  onChange={(next) => setStt((s) => ({
                    ...s,
                    settings: { ...s.settings, turn_detection: next },
                  }))}
                />
              </div>
            </details>
            <CleanupCallout
              items={sttIssues}
              onApply={() => setStt((s) => ({ ...s, language: "" }))}
            />
          </SectionCard>

          {/* 2 — Language Model */}
          <SectionCard
            title="Language Model" sub="Generates the assistant's replies"
            actions={
              <TestControl
                state={tests.llm} disabled={!llm.provider || !canManage} title={noPermTitle}
                onTest={() => void runTest("llm", { capability: "llm", provider: llm.provider, model: llm.model || undefined })}
              />
            }
          >
            <ProviderSelect
              label="Provider" capability="llm" providers={llmProviders}
              value={llm.provider} onChange={(v) => void changeLlmProvider(v)}
            />
            <ModelSelect
              label="Model" ariaLabel="LLM model" models={llmModels} value={llm.model}
              disabled={!llm.provider} onChange={changeLlmModel}
            />
            <ParamFields
              key={`llm:${llm.provider}:${llm.model}`}
              schema={llmSchema} values={llm.settings}
              onChange={(next) => setLlm((s) => ({ ...s, settings: next }))}
            />
          </SectionCard>

          {/* 3 — Text-to-Speech */}
          <SectionCard
            title="Text-to-Speech" sub="Speaks the assistant's replies"
            actions={
              <TestControl
                state={tests.tts} disabled={!tts.provider || !canManage} title={noPermTitle}
                onTest={() => void runTest("tts", { capability: "tts", provider: tts.provider, model: tts.model || undefined, voice: tts.voice || undefined })}
              />
            }
          >
            <ProviderSelect
              label="Provider" capability="tts" providers={ttsProviders}
              value={tts.provider} onChange={(v) => void changeTtsProvider(v)}
            />
            <ModelSelect
              label="Model" ariaLabel="TTS model" models={ttsModels} value={tts.model}
              disabled={!tts.provider} onChange={changeTtsModel}
            />
            {tts.provider && (
              <Field label="Voice" plain>
                <SearchableSelect
                  options={withCurrent(voiceSelectOptions(tts.provider, tts.model), tts.voice)}
                  value={tts.voice}
                  onChange={(v) => setTts((s) => ({ ...s, voice: v }))}
                  placeholder={voicesRef.current[tts.provider] ? "Select voice…" : "Loading voices…"}
                  searchPlaceholder="Search voices…"
                  ariaLabel="TTS voice"
                />
              </Field>
            )}
            {/* Only the selected provider's schema is rendered — never both providers' settings. */}
            <ParamFields
              key={`tts:${tts.provider}:${tts.model}`}
              schema={ttsSchema} values={tts.settings}
              onChange={(next) => setTts((s) => ({ ...s, settings: next }))}
            />
            {/* Widget-backed schema entries (pronunciation dictionary) render
                as their specialized control, never as a raw provider-id box. */}
            {Object.entries(ttsSchema).filter(([, s]) => s.widget === "dictionary").map(([key, spec]) => (
              <DictionaryField
                key={`dict:${tts.provider}:${tts.model}:${key}`}
                value={typeof tts.settings[key] === "string" ? (tts.settings[key] as string) : null}
                disabled={!canManage}
                help={spec.help}
                onChange={(dictId) => setTts((s) => {
                  const settings = { ...s.settings };
                  if (dictId) settings[key] = dictId;
                  else delete settings[key];
                  return { ...s, settings };
                })}
              />
            ))}
            <CleanupCallout items={ttsIssues} onApply={() => setTts((s) => ({ ...s, voice: "" }))} />
            <div>
              <Button
                icon="play"
                disabled={!canManage || !tts.provider || !tts.model || !tts.voice}
                title={noPermTitle ?? (!tts.voice ? "Pick a provider, model and voice first" : undefined)}
                onClick={() => setPreview({
                  provider: tts.provider, model: tts.model, voice: tts.voice,
                  language: defaultLocale || bot.languages[0] || "", params: tts.settings,
                  tuning,
                  resolveOverride,
                  onApply: (draft) => {
                    setTts({
                      provider: draft.provider, model: draft.model,
                      voice: draft.voice, settings: draft.settings,
                    });
                    setTuning({
                      speed: draft.speed, pauseMs: draft.pauseMs,
                      empathy: draft.empathy, energy: draft.energy,
                    });
                  },
                })}
              >
                Preview voice
              </Button>
            </div>
          </SectionCard>
        </div>

        {/* ── right column: delivery, mapping, transport, fallback ── */}
        <div className="col gap-16">
          {/* Delivery tuning (compact) */}
          <SectionCard title="Delivery tuning" sub="How the assistant sounds, independent of engine">
            <Slider label="Speaking speed" value={tuning.speed} min={0.7} max={1.4} step={0.05} fmt={(v) => `${v.toFixed(2)}×`}
              hint="Applies across all voice providers — the single speed control for this bot."
              onChange={(v) => setTuning((t) => ({ ...t, speed: v }))} />
            <Slider label="Pause between sentences" value={tuning.pauseMs} min={100} max={900} step={50} fmt={(v) => `${v}ms`}
              hint="Silence inserted between assistant sentences."
              onChange={(v) => setTuning((t) => ({ ...t, pauseMs: v }))} />
            <Slider label="Empathy" value={tuning.empathy} min={0} max={100} step={5} fmt={(v) => `${v}%`}
              hint="Influences wording and acknowledgement in generated replies."
              onChange={(v) => setTuning((t) => ({ ...t, empathy: v }))} />
            <Slider label="Energy" value={tuning.energy} min={0} max={100} step={5} fmt={(v) => `${v}%`}
              hint="How calm or lively replies feel; native voice support varies by provider."
              onChange={(v) => setTuning((t) => ({ ...t, energy: v }))} />
          </SectionCard>

          {settingsQ.data?.humanSpeechInherited && settingsQ.data.humanSpeechInheritedSources && (
            <SectionCard title="Natural conversation" sub="Sparse bot overrides with tenant/platform inheritance">
              <HumanSpeechSettingsEditor
                scope="bot"
                override={humanSpeech}
                inherited={settingsQ.data.humanSpeechInherited}
                inheritedSources={settingsQ.data.humanSpeechInheritedSources}
                disabled={!canManage}
                onChange={setHumanSpeech}
              />
            </SectionCard>
          )}

          {/* 4 — Per-language voices */}
          <SectionCard title="Per-language voices" sub="Override which engine answers in each language">
            <Field label="Default language" plain hint='Stored as languageVoiceMap["default"] — used when the caller language is unknown'>
              <select
                className="select" value={defaultLocale} aria-label="Default language"
                onChange={(e) => setLangMap((m) => {
                  const next = { ...m };
                  if (e.target.value) next["default"] = e.target.value;
                  else delete next["default"];
                  return next;
                })}
              >
                <option value="">Not set</option>
                {bot.languages.map((l) => <option key={l} value={l}>{langLabel(l)} ({l})</option>)}
              </select>
            </Field>

            {bot.languages.length === 0 ? (
              <p className="t-sub" style={{ margin: 0 }}>
                This bot has no languages yet — add languages in the Overview tab to configure per-language voices.
              </p>
            ) : (
              <ul className="lang-voice-list" aria-label="Per-language voice overrides">
                {bot.languages.map((locale) => {
                  const entry = langMap[locale];
                  const override = entry && typeof entry === "object" ? entry : null;
                  const legacy = typeof entry === "string" && entry ? entry : null;
                  /* Per-language overrides run inside the realtime streaming
                     router — every active model is listed so the catalog is
                     visible, with non-streaming ones disabled + the reason. */
                  const rowModels = override
                    ? modelsFor("tts", override.provider)
                    : [];
                  const rowModelsLoaded = override ? modelsRef.current[`tts:${override.provider}`] !== undefined : false;
                  const providerKnown = !override || ttsProviders.some((p) => p.code === override.provider);
                  const modelKnown = !override?.model || rowModels.some((m) => m.code === override.model);
                  const status = rowStatus(locale);
                  const paramsSummary = override?.params && Object.keys(override.params).length > 0
                    ? Object.entries(override.params).map(([k, v]) => `${k}: ${String(v)}`).join(" · ")
                    : null;
                  return (
                    <li key={locale} className="lang-voice-row">
                      <div className="lang-voice-head">
                        <div className="lang-voice-lang">
                          <span className="t-strong" style={{ fontSize: 13 }}>{langLabel(locale)}</span>
                          <span className="t-micro">
                            {locale}
                            {locale === defaultLocale ? " · default language" : ""}
                            {langCatalog[locale] && !langCatalog[locale].enabled ? " · disabled on platform" : ""}
                          </span>
                        </div>
                        {/* Chip + actions form ONE right-aligned unit so a
                            narrow card wraps them together under the name —
                            never a chip on one line and buttons on another. */}
                        <div className="lang-voice-meta">
                        <StatusChip status={status.chip} label={status.label} />
                        <div className="lang-voice-actions">
                          <Button
                            size="sm" icon="play"
                            disabled={!canManage || !override?.provider || !override.model || !override.voice}
                            title={noPermTitle ?? (override?.voice ? "Generate a real audio preview" : "Pick a provider, model and voice to preview")}
                            aria-label={`Preview voice for ${langLabel(locale)}`}
                            onClick={() => override && setPreview({
                              provider: override.provider, model: override.model, voice: override.voice,
                              language: locale, params: override.params, tuning,
                              requiresStreaming: true,
                              resolveOverride,
                              defaultEngine: {
                                provider: tts.provider, model: tts.model,
                                voice: tts.voice, params: tts.settings,
                              },
                              /* Engine + params go to this language row;
                                 delivery values are bot-level and stage the
                                 same way as the default-engine preview. */
                              onApply: (draft) => {
                                setRowField(locale, {
                                  provider: draft.provider, model: draft.model,
                                  voice: draft.voice, params: draft.settings,
                                });
                                setTuning({
                                  speed: draft.speed, pauseMs: draft.pauseMs,
                                  empathy: draft.empathy, energy: draft.energy,
                                });
                              },
                            })}
                          >
                            Preview voice
                          </Button>
                          <Button
                            size="sm" variant="ghost" icon="undo"
                            disabled={!canManage || (!override && !legacy)}
                            title={noPermTitle ?? "Remove this override — the language falls back to the default engine"}
                            aria-label={`Reset voice override for ${langLabel(locale)}`}
                            onClick={() => clearRow(locale)}
                          >
                            Reset
                          </Button>
                        </div>
                        </div>
                      </div>

                      <div className="lang-voice-fields">
                        <Field label="Provider" plain>
                          <select
                            className="select" value={override?.provider ?? ""}
                            aria-label={`Voice provider for ${locale}`}
                            onChange={(e) => void setRowProvider(locale, e.target.value)}
                          >
                            <option value="">Inherit default</option>
                            {override && !providerKnown && (
                              <option value={override.provider} disabled>{override.provider} (unavailable)</option>
                            )}
                            {ttsProviders.map((p) => <option key={p.code} value={p.code}>{p.name}</option>)}
                          </select>
                        </Field>
                        <Field label="Model" plain>
                          <select
                            className="select" value={override?.model ?? ""}
                            aria-label={`Voice model for ${locale}`}
                            disabled={!override}
                            onChange={(e) => setRowModel(locale, e.target.value)}
                          >
                            <option value="">{override ? "Select model" : "—"}</option>
                            {override?.model && !modelKnown && (
                              /* Until the catalog answers, the saved value stays selectable-as-is;
                                 once loaded and absent it is pinned as an unavailable entry. */
                              <option value={override.model} disabled={rowModelsLoaded}>
                                {override.model}{rowModelsLoaded ? " (unavailable)" : ""}
                              </option>
                            )}
                            {rowModels.map((m) => (
                              <option
                                key={m.code} value={m.code} disabled={!m.streaming}
                                title={!m.streaming
                                  ? "Live calls switch languages in realtime — this model only synthesizes over REST."
                                  : undefined}
                              >
                                {m.displayName}{m.isDefault ? " (default)" : ""}{!m.streaming ? NO_STREAMING_SUFFIX : ""}
                              </option>
                            ))}
                          </select>
                        </Field>
                        <Field label="Voice" plain>
                          <SearchableSelect
                            options={override ? withCurrent(voiceSelectOptions(override.provider, override.model, locale), override.voice) : []}
                            value={override?.voice ?? ""}
                            onChange={(v) => setRowField(locale, { voice: v })}
                            placeholder={override ? (voicesRef.current[override.provider] ? "Select voice…" : "Loading voices…") : "Inherited"}
                            searchPlaceholder="Search voices…"
                            disabled={!override}
                            ariaLabel={`Voice for ${locale}`}
                          />
                        </Field>
                      </div>

                      {!override && !legacy && (
                        <span className="t-micro">Uses {primaryEngineSummary}.</span>
                      )}
                      {paramsSummary && (
                        <span className="t-micro" title="Provider-specific synthesis settings stored with this override">
                          Settings: {paramsSummary}
                        </span>
                      )}
                      {legacy && (
                        <span className="t-micro">
                          Legacy voice reference <code>{legacy}</code> — pick a provider to convert this override, or Reset to remove it.
                        </span>
                      )}
                      {status.message && (
                        <span className="field-error" role="alert">
                          <Icon name="alert" size={12} />{status.message}
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
            <CleanupCallout
              items={mappingIssues.map((i) => i.message)}
              onApply={() => setLangMap((m) => {
                const next = { ...m };
                for (const issue of mappingIssues) {
                  const entry = next[issue.locale];
                  if (entry && typeof entry === "object") next[issue.locale] = { ...entry, voice: "" };
                }
                return next;
              })}
            />
          </SectionCard>

          {/* 5 — Audio & Telephony */}
          <SectionCard title="Audio & Telephony" sub="Transport codecs and sample rates">
            <div className="grid grid-2" style={{ gap: 12 }}>
              <Field label="Browser codec" plain hint="Fixed — browser sessions stream linear PCM">
                <input className="input" value="linear16" readOnly disabled aria-label="Browser codec" />
              </Field>
              <Field label="Browser sample rate" plain>
                <select
                  className="select" value={audio.browser?.sampleRate ?? 16000} aria-label="Browser sample rate"
                  onChange={(e) => setAudio((a) => ({ ...a, browser: { codec: "linear16", sampleRate: Number(e.target.value) } }))}
                >
                  <option value={16000}>16,000 Hz</option>
                  <option value={24000}>24,000 Hz</option>
                </select>
              </Field>
              <Field label="Telephony codec" plain>
                <select
                  className="select" value={audio.telephony?.codec ?? "mulaw"} aria-label="Telephony codec"
                  onChange={(e) => setAudio((a) => ({ ...a, telephony: { codec: e.target.value, sampleRate: 8000 } }))}
                >
                  <option value="mulaw">μ-law (mulaw)</option>
                  <option value="alaw">A-law (alaw)</option>
                  <option value="linear16">linear16</option>
                </select>
              </Field>
              <Field label="Telephony sample rate" plain hint="Fixed — PSTN carriers stream 8 kHz">
                <input className="input" value="8,000 Hz" readOnly disabled aria-label="Telephony sample rate" />
              </Field>
            </div>
          </SectionCard>

          {/* 6 — Fallback & Reliability */}
          <SectionCard title="Fallback & Reliability" sub="Secondary TTS engine used when the primary fails">
            <Field label="Fallback provider" plain hint="Must differ from the primary TTS provider">
              <select
                className="select" value={fallback.provider} aria-label="Fallback TTS provider"
                onChange={(e) => void changeFallbackProvider(e.target.value)}
              >
                <option value="">None</option>
                {ttsProviders.filter((p) => p.code !== tts.provider).map((p) => (
                  <option key={p.code} value={p.code}>
                    {p.name}{p.requiresApiKey && !p.hasCredentials ? " — key missing" : ""}
                  </option>
                ))}
              </select>
            </Field>
            {fallback.provider && (
              <>
                <ModelSelect
                  label="Fallback model" models={fallbackModels} value={fallback.model}
                  requireStreaming
                  onChange={(v) => setFallback((f) => ({ ...f, model: v }))}
                />
                <Field label="Fallback voice" plain>
                  <SearchableSelect
                    options={withCurrent(voiceSelectOptions(fallback.provider, fallback.model), fallback.voice)}
                    value={fallback.voice}
                    onChange={(v) => setFallback((f) => ({ ...f, voice: v }))}
                    placeholder={voicesRef.current[fallback.provider] ? "Select voice…" : "Loading voices…"}
                    searchPlaceholder="Search voices…"
                    ariaLabel="Fallback voice"
                  />
                </Field>
              </>
            )}
            <CleanupCallout items={fallbackIssues} onApply={() => setFallback((f) => ({ ...f, voice: "" }))} />
          </SectionCard>
        </div>
      </div>

      {/* ── save bar ── */}
      {saveErrors.length > 0 && (
        <Callout tone="critical" title="The catalog rejected this configuration">
          <ul style={{ margin: 0, paddingLeft: 16 }}>{saveErrors.map((e) => <li key={e}>{e}</li>)}</ul>
        </Callout>
      )}
      {saveWarnings.length > 0 && (
        <Callout tone="warning" title={warningsFromSave ? "Saved with warnings" : "Warnings"}>
          <ul style={{ margin: 0, paddingLeft: 16 }}>{saveWarnings.map((w) => <li key={w}>{w}</li>)}</ul>
        </Callout>
      )}
      <div className="row gap-12" style={{ justifyContent: "flex-end" }}>
        <Button
          icon="check-circle" onClick={() => void validate()} busy={validating}
          disabled={validating || saving || !canManage} title={noPermTitle}
        >
          Validate
        </Button>
        <Button
          variant="primary" icon="check" onClick={() => void save()} busy={saving}
          disabled={saving || validating || !canManage} title={noPermTitle}
        >
          Save voice settings
        </Button>
      </div>

      {/* 7 — Preview modal */}
      <PreviewVoiceModal
        ctx={preview}
        onClose={() => setPreview(null)}
        ttsProviders={ttsProviders}
        modelsFor={(p) => modelsFor("tts", p)}
        voicesFor={(p) => voicesRef.current[p]}
        ensureModels={(p) => ensureModels("tts", p)}
        ensureVoices={ensureVoices}
        languages={bot.languages}
      />
    </div>
  );
}

/* ============================================================
   Section / field building blocks
   ============================================================ */

function SectionCard({ title, sub, actions, children }: {
  title: string; sub?: string; actions?: ReactNode; children: ReactNode;
}) {
  return (
    <div className="card">
      <div className="card-header">
        <div className="col gap-2">
          <span className="card-title">{title}</span>
          {sub && <span className="t-micro">{sub}</span>}
        </div>
        {actions}
      </div>
      <div className="col" style={{ padding: 18, gap: 14 }}>{children}</div>
    </div>
  );
}

function ProviderSelect({ label, capability, providers, value, onChange }: {
  label: string; capability: VoiceCapability; providers: ProviderInfo[];
  value: string; onChange: (v: string) => void;
}) {
  const selected = providers.find((p) => p.code === value);
  const keyMissing = selected ? selected.requiresApiKey && !selected.hasCredentials : false;
  return (
    <Field label={label} plain hint={selected?.description || undefined}>
      <div className="row gap-6">
        <select
          className="select grow" value={value} aria-label={`${capability.toUpperCase()} provider`}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">Platform default</option>
          {value && !providers.some((p) => p.code === value) && <option value={value}>{value}</option>}
          {providers.map((p) => (
            <option key={p.code} value={p.code}>
              {p.name}{p.requiresApiKey && !p.hasCredentials ? " — key missing" : ""}
            </option>
          ))}
        </select>
        {keyMissing && (
          <span className="chip chip-warning" title="No API key configured — sessions will fail until it is set">
            <Icon name="key" size={11} />key missing
          </span>
        )}
      </div>
    </Field>
  );
}

/** Suffix explaining why a model is not selectable in a realtime-only slot. */
export const NO_STREAMING_SUFFIX = " — no realtime streaming";

function ModelSelect({ label, models, value, onChange, disabled, ariaLabel, requireStreaming }: {
  label: string; models: ProviderModelInfo[]; value: string;
  onChange: (v: string) => void; disabled?: boolean;
  /** Distinct accessible name — the visible label is "Model" in every engine section. */
  ariaLabel?: string;
  /** This slot runs inside the realtime streaming router (fallback engine,
      per-language override): every provider model is LISTED so the catalog
      is visible, but non-streaming ones are disabled with the reason. */
  requireStreaming?: boolean;
}) {
  /* Concise catalog description of the selected model, plus a realtime note
     for TTS models the provider cannot stream over its realtime socket
     (e.g. Eleven v3 — replies synthesize per segment over REST). */
  const selected = models.find((m) => m.code === value);
  const hint = selected
    ? [selected.description, selected.capability === "tts" && !selected.streaming
        ? "No realtime streaming — replies are synthesized per segment (higher latency)."
        : null].filter(Boolean).join(" ") || undefined
    : undefined;
  return (
    <Field label={label} plain hint={hint}>
      <select
        className="select" value={value} disabled={disabled} aria-label={ariaLabel ?? label}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">{disabled ? "Provider default" : "Select model"}</option>
        {value && !models.some((m) => m.code === value) && <option value={value}>{value}</option>}
        {models.map((m) => {
          const blocked = Boolean(requireStreaming && !m.streaming);
          return (
            <option
              key={m.code} value={m.code} disabled={blocked}
              title={blocked
                ? "Live calls switch engines in realtime — this model only synthesizes over REST."
                : m.description ?? undefined}
            >
              {m.displayName}{m.isDefault ? " (default)" : ""}{blocked ? NO_STREAMING_SUFFIX : ""}
            </option>
          );
        })}
      </select>
    </Field>
  );
}

function TestControl({ state, onTest, disabled, title }: {
  state?: { busy: boolean; result: ProviderTestResult | null };
  onTest: () => void; disabled?: boolean; title?: string;
}) {
  const result = state?.result ?? null;
  return (
    <div className="row gap-6">
      {result && (result.ok ? (
        <span className="chip chip-good">
          <Icon name="check-circle" size={11} />
          {result.latencyMs !== undefined ? `${Math.round(result.latencyMs)} ms` : "OK"}
        </span>
      ) : (
        <span className="chip chip-critical" title={result.message}>
          <Icon name="x-circle" size={11} />{result.error ?? "failed"}
        </span>
      ))}
      <Button size="sm" icon="plug" busy={state?.busy} onClick={onTest} disabled={disabled} title={title}>
        Test
      </Button>
    </div>
  );
}

function CleanupCallout({ items, onApply }: { items: string[]; onApply: () => void }) {
  if (items.length === 0) return null;
  return (
    <Callout tone="warning" title="Selections no longer valid">
      <div className="col gap-6">
        <ul style={{ margin: 0, paddingLeft: 16 }}>{items.map((m) => <li key={m}>{m}</li>)}</ul>
        <div><Button size="sm" onClick={onApply}>Apply changes</Button></div>
      </div>
    </Callout>
  );
}

/* ============================================================
   Delivery tuning slider (kept from the previous tab)
   ============================================================ */

function Slider({ label, value, min, max, step, fmt, hint, onChange }: {
  label: string; value: number; min: number; max: number; step: number;
  fmt: (v: number) => string; hint?: string; onChange: (v: number) => void;
}) {
  return (
    <label className="col gap-6">
      <span className="row-between">
        <span className="field-label">{label}</span>
        <span className="t-num t-strong" style={{ fontSize: 12.5 }}>{fmt(value)}</span>
      </span>
      <input
        type="range"
        min={min} max={max} step={step} value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ accentColor: "var(--brand-500)", width: "100%" }}
        aria-label={label}
      />
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

/* ============================================================
   Voice preview modal — real synthesis via /providers/tts-preview
   ============================================================ */

function PreviewVoiceModal({ ctx, onClose, ttsProviders, modelsFor, voicesFor, ensureModels, ensureVoices, languages }: {
  ctx: PreviewContext | null;
  onClose: () => void;
  ttsProviders: ProviderInfo[];
  modelsFor: (provider: string) => ProviderModelInfo[];
  voicesFor: (provider: string) => VoiceOption[] | undefined;
  ensureModels: (provider: string) => Promise<ProviderModelInfo[]>;
  ensureVoices: (provider: string) => Promise<VoiceOption[]>;
  languages: string[];
}) {
  const [form, setForm] = useState({ provider: "", model: "", voice: "", language: "", text: "" });
  /* Draft provider settings — local to the modal until the user applies them.
     Moving a slider never persists anything; it only changes what the next
     Generate synthesizes. */
  const [draft, setDraft] = useState<ProviderSettings>({});
  /* Bot-level Delivery tuning drafts (same persisted fields production uses). */
  const [speed, setSpeed] = useState(1);
  const [pause, setPause] = useState(350);
  const [empathy, setEmpathy] = useState(50);
  const [energy, setEnergy] = useState(50);
  /* Set when a language change resolved to a per-language override engine —
     the modal then previews that engine, exactly like a live call would. */
  const [overrideLocale, setOverrideLocale] = useState<string | null>(null);
  const [applied, setApplied] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [result, setResult] = useState<TtsPreviewResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const draftFor = (provider: string, model: string, params: ProviderSettings | undefined) => {
    const schema = stripDeliverySpeedParams(
      modelsFor(provider).find((m) => m.code === model)?.paramsSchema,
    );
    return reconcileSettings(schema, stripDeliverySpeedParams(params ?? {}));
  };

  /* Prefill from the launching context each time the modal opens. */
  useEffect(() => {
    if (!ctx) return;
    const voice = findVoice(voicesFor(ctx.provider), ctx.voice);
    setForm({
      provider: ctx.provider,
      model: ctx.model,
      voice: ctx.voice,
      language: ctx.language || languages[0] || "",
      text: voice?.sampleText || DEFAULT_SAMPLE_TEXT,
    });
    /* Start from the saved/staged values for this engine, reconciled against
       the model actually selected — never from another model's leftovers. */
    setDraft(draftFor(ctx.provider, ctx.model, ctx.params));
    setSpeed(ctx.tuning?.speed ?? 1);
    setPause(ctx.tuning?.pauseMs ?? 350);
    setEmpathy(ctx.tuning?.empathy ?? 50);
    setEnergy(ctx.tuning?.energy ?? 50);
    /* Launched from a per-language row: the modal starts ON that override,
       so a later switch to an inheriting locale must restore the default. */
    const launchedOnOverride = Boolean(
      ctx.defaultEngine && (
        ctx.defaultEngine.provider !== ctx.provider
        || ctx.defaultEngine.model !== ctx.model
        || ctx.defaultEngine.voice !== ctx.voice
      ),
    );
    setOverrideLocale(launchedOnOverride ? (ctx.language || null) : null);
    setApplied(false);
    setResult(null);
    setError(null);
    setPlaying(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ctx]);

  useEffect(() => () => { audioRef.current?.pause(); }, []);

  const stop = () => {
    audioRef.current?.pause();
    setPlaying(false);
  };
  const close = () => { stop(); onClose(); };

  /* A provider switch has no settings to carry over — the new provider's
     parameters are entirely different — so the draft resets to the new
     default model's defaults. */
  const changeProvider = async (provider: string) => {
    setForm((f) => ({ ...f, provider, model: "", voice: "" }));
    setDraft({});
    setOverrideLocale(null);
    setApplied(false);
    if (!provider) return;
    void ensureVoices(provider);
    const models = await ensureModels(provider);
    const def = models.find((m) => m.isDefault) ?? models[0];
    setForm((f) => (f.provider === provider && !f.model ? { ...f, model: def?.code ?? "" } : f));
    setDraft(schemaDefaults(stripDeliverySpeedParams(def?.paramsSchema)));
  };

  /* A model switch keeps settings the new model also defines (clamped into its
     range) and drops the rest — v2's pitch/loudness disappear on v3, and v3's
     temperature appears with its default. */
  const changeModel = (model: string) => {
    const schema = stripDeliverySpeedParams(
      modelsFor(form.provider).find((m) => m.code === model)?.paramsSchema,
    );
    setForm((f) => ({ ...f, model, voice: "" }));
    setDraft((d) => reconcileSettings(schema, d));
    setOverrideLocale(null);
    setApplied(false);
  };

  /* A language change resolves the SAME engine a live call would use for
     that locale: a complete per-language override switches the previewed
     provider/model/voice (+ its params, over the base settings when the
     override runs the identical model, mirroring the runtime); leaving an
     override restores the default engine. A locale with no override leaves
     the user's manual selections alone. */
  const changeLanguage = async (locale: string) => {
    setForm((f) => ({ ...f, language: locale }));
    setApplied(false);
    if (!ctx) return;
    const fallback = ctx.defaultEngine
      ?? { provider: ctx.provider, model: ctx.model, voice: ctx.voice, params: ctx.params };
    const override = ctx.resolveOverride?.(locale) ?? null;
    const isDefaultEngine = (e: { provider: string; model: string; voice: string }) =>
      e.provider === fallback.provider && e.model === fallback.model && e.voice === fallback.voice;
    if (override === null || isDefaultEngine(override)) {
      if (overrideLocale !== null) {
        setForm((f) => ({
          ...f, language: locale,
          provider: fallback.provider, model: fallback.model, voice: fallback.voice,
        }));
        setDraft(draftFor(fallback.provider, fallback.model, fallback.params));
        setOverrideLocale(null);
      }
      return;
    }
    void ensureVoices(override.provider);
    await ensureModels(override.provider);
    const sameEngine = override.provider === fallback.provider && override.model === fallback.model;
    setForm((f) => ({
      ...f, language: locale,
      provider: override.provider, model: override.model, voice: override.voice,
    }));
    /* Runtime semantics: base tts_settings apply only when the override runs
       the default provider+model; otherwise the override's params stand alone. */
    setDraft(draftFor(
      override.provider, override.model,
      sameEngine ? { ...(fallback.params ?? {}), ...(override.params ?? {}) } : override.params,
    ));
    setOverrideLocale(locale);
  };

  const generate = async () => {
    setGenerating(true);
    setError(null);
    setResult(null);
    try {
      const r = await generateTtsPreview({
        provider: form.provider, model: form.model, voice: form.voice,
        language: form.language, text: form.text.trim(),
        /* The unsaved draft is what gets synthesized — that is the point of
           tuning here. It is already reconciled to the selected model, so no
           parameter from a previous provider/model can ride along. */
        params: draft,
        /* Delivery tuning DRAFTS: canonical speed, sentence pause and native
           energy mapping are applied server-side with the live-call logic
           (shared/providers/tts/delivery.py). Empathy shapes live LLM replies
           and cannot rewrite fixed preview text, so it is not sent. */
        speed: speedRange ? speed : undefined,
        pauseMs: pause,
        energy,
      });
      setResult(r);
      const audio = new Audio(`data:${r.mimeType};base64,${r.audioBase64}`);
      audioRef.current = audio;
      audio.onended = () => setPlaying(false);
      setPlaying(true);
      await audio.play();
    } catch (e) {
      setPlaying(false);
      setError(e instanceof Error ? e.message : "Preview failed");
    } finally {
      setGenerating(false);
    }
  };

  const models = modelsFor(form.provider);
  const selectedModel = models.find((m) => m.code === form.model);
  /* Only the selected model's schema is ever rendered or sent. Delivery owns
     pace/speed, so those are shown as the single speaking-speed control below
     instead of as raw provider parameters. */
  const settingsSchema = stripDeliverySpeedParams(selectedModel?.paramsSchema);
  const speedRange = effectiveSpeedRange(selectedModel);
  const settingsCount = Object.keys(settingsSchema).length;
  const voices = voicesFor(form.provider);
  const voiceOptions = withCurrent(
    (voices ?? [])
      .filter((v) => voiceSupportsModel(v, form.model))
      .map((v) => ({
        value: v.id,
        label: v.name,
        sub: [v.gender, v.locale].filter(Boolean).join(" · "),
        disabled: v.status === "unavailable",
      })),
    form.voice,
  );
  const textLen = form.text.trim().length;
  const canGenerate = Boolean(form.provider && form.model && form.voice && textLen > 0 && textLen <= 500);
  /* Applying only stages the engine + settings on the page; it does not need
     preview text to be valid, but does need a complete engine selection.
     While the modal previews a per-language override that is NOT the engine
     it was launched to edit, applying would clobber the launch target with a
     different locale's engine — blocked with an explanatory title. */
  const launchedOnOverride = Boolean(ctx?.defaultEngine && (
    ctx.defaultEngine.provider !== ctx.provider
    || ctx.defaultEngine.model !== ctx.model
    || ctx.defaultEngine.voice !== ctx.voice
  ));
  const applyingForeignOverride = overrideLocale !== null
    && !(launchedOnOverride && overrideLocale === ctx?.language);
  /* A per-language target only accepts realtime-streaming engines (the
     backend enforces the same rule on save) — a stale non-streaming model
     can still be PREVIEWED, just never applied. */
  const applyingNonStreaming = Boolean(
    ctx?.requiresStreaming && selectedModel && !selectedModel.streaming,
  );
  const canApply = Boolean(form.provider && form.model && form.voice)
    && !applyingForeignOverride && !applyingNonStreaming;
  /* Schema entries rendered by specialized widgets (dictionary selector). */
  const widgetEntries = Object.entries(settingsSchema).filter(([, s]) => s.widget === "dictionary");
  const paramCount = settingsCount - widgetEntries.length;

  return (
    <Modal
      open={ctx !== null}
      onClose={close}
      wide
      title="Preview voice"
      sub="Tune the voice, then enter sample text and generate real audio"
      footer={
        <>
          <Button variant="ghost" onClick={close}>Close</Button>
          <Button icon="pause" onClick={stop} disabled={!playing}>Stop</Button>
          {ctx?.onApply && (
            <Button
              icon={applied ? "check" : "check-circle"}
              disabled={!canApply}
              title={canApply
                ? "Stage these settings on the Voice tab — still needs Save voice settings"
                : applyingForeignOverride
                  ? "This language uses a per-language override — edit it from the Per-language voices section"
                  : applyingNonStreaming
                    ? `${selectedModel?.displayName ?? form.model} does not support realtime streaming — per-language overrides need a streaming model`
                    : "Pick a provider, model and voice first"}
              onClick={() => {
                ctx.onApply?.({
                  provider: form.provider, model: form.model, voice: form.voice,
                  settings: draft, speed, pauseMs: pause, empathy, energy,
                });
                setApplied(true);
              }}
            >
              {applied ? "Applied" : "Apply settings"}
            </Button>
          )}
          <Button
            variant="primary" icon="play" busy={generating}
            onClick={() => void generate()}
            disabled={generating || playing || !canGenerate}
            title={canGenerate ? undefined : "Pick provider, model, voice and up to 500 characters of text"}
          >
            {generating ? "Generating…" : "Generate"}
          </Button>
        </>
      }
    >
      <div className="col gap-12">
        {/* 1 — Engine selection */}
        <div className="preview-grid">
          <Field label="Provider" plain>
            <select
              className="select" value={form.provider} aria-label="Preview provider"
              onChange={(e) => void changeProvider(e.target.value)}
            >
              {form.provider && !ttsProviders.some((p) => p.code === form.provider) && (
                <option value={form.provider}>{form.provider}</option>
              )}
              {ttsProviders.map((p) => <option key={p.code} value={p.code}>{p.name}</option>)}
            </select>
          </Field>
          <Field label="Model" plain>
            <select
              className="select" value={form.model} aria-label="Preview model"
              onChange={(e) => changeModel(e.target.value)}
            >
              <option value="">Select model</option>
              {form.model && !models.some((m) => m.code === form.model) && (
                <option value={form.model}>{form.model}</option>
              )}
              {models.map((m) => {
                /* Previewing a non-streaming model is always allowed; when
                   the APPLY TARGET is a per-language row it cannot be staged,
                   so the option is disabled with the reason. */
                const blocked = Boolean(ctx?.requiresStreaming && !m.streaming);
                return (
                  <option
                    key={m.code} value={m.code} disabled={blocked}
                    title={blocked
                      ? "Per-language overrides run in realtime — this model only synthesizes over REST."
                      : m.description ?? undefined}
                  >
                    {m.displayName}{blocked ? NO_STREAMING_SUFFIX : ""}
                  </option>
                );
              })}
            </select>
          </Field>
          <Field label="Voice" plain>
            <SearchableSelect
              options={voiceOptions}
              value={form.voice}
              onChange={(v) => {
                const voice = findVoice(voices, v);
                setForm((f) => ({
                  ...f, voice: v,
                  text: f.text.trim() ? f.text : (voice?.sampleText || DEFAULT_SAMPLE_TEXT),
                }));
              }}
              placeholder={voices ? "Select voice…" : "Loading voices…"}
              searchPlaceholder="Search voices…"
              ariaLabel="Preview voice"
            />
          </Field>
          <Field
            label="Language" plain
            hint={overrideLocale ? undefined : "A language with a per-language override previews that engine, like a real call."}
          >
            <select
              className="select" value={form.language} aria-label="Preview language"
              onChange={(e) => void changeLanguage(e.target.value)}
            >
              {form.language && !languages.includes(form.language) && (
                <option value={form.language}>{form.language}</option>
              )}
              {languages.map((l) => <option key={l} value={l}>{l}</option>)}
            </select>
          </Field>
        </div>
        {overrideLocale && (
          <p className="t-micro" style={{ margin: 0 }} data-testid="override-hint">
            <Icon name="info" size={12} /> {overrideLocale} uses its per-language voice override —
            this preview resolved the same engine a real call would use.
          </p>
        )}

        {/* 2 — Provider-specific voice controls (catalog paramsSchema) */}
        <div className="preview-settings">
          <div className="row-between" style={{ alignItems: "baseline" }}>
            <span className="t-label">Voice settings</span>
            {form.model && paramCount > 0 && (
              <button
                type="button" className="param-reset" style={{ position: "static" }}
                aria-label="Reset all voice settings to defaults"
                onClick={() => {
                  setDraft((d) => ({ ...schemaDefaults(settingsSchema), ...Object.fromEntries(widgetEntries.map(([k]) => [k, d[k]])) }));
                  setApplied(false);
                }}
              >
                Reset all to defaults
              </button>
            )}
          </div>
          {!form.model ? (
            <p className="field-hint" style={{ margin: 0 }}>Select a model to tune its voice settings.</p>
          ) : (
            <div className="col gap-12" style={{ marginTop: 10 }}>
              <ParamFields
                key={`preview:${form.provider}:${form.model}`}
                schema={settingsSchema}
                values={draft}
                showReset
                onChange={(next) => { setDraft(next); setApplied(false); }}
              />
              {paramCount === 0 && (
                <p className="field-hint" style={{ margin: 0 }}>
                  This model exposes no additional tunable settings.
                </p>
              )}
            </div>
          )}
        </div>

        {/* 3 — Delivery tuning drafts (bot-level persisted fields). The SAME
            editable controls regardless of which Preview button opened the
            modal — these values are bot-level, so every launch point stages
            them to the same place. */}
        <div className="preview-settings">
          <span className="t-label">Delivery tuning</span>
          <div className="col gap-12" style={{ marginTop: 10 }}>
            <div className="preview-grid">
              {speedRange ? (
                <Slider
                  label="Speaking speed" value={speed}
                  min={speedRange[0]} max={speedRange[1]} step={0.05}
                  fmt={(v) => `${v.toFixed(2)}×`}
                  hint={`Sent as ${form.provider === "sarvam" ? "pace" : "speed"} (${speedRange[0]}–${speedRange[1]}×) — the bot's single speed control.`}
                  onChange={(v) => { setSpeed(v); setApplied(false); }}
                />
              ) : (
                <div className="col gap-6">
                  <span className="field-label">Speaking speed</span>
                  <span className="field-hint">
                    {selectedModel
                      ? `${selectedModel.displayName} has no speed control — speaking speed is not applied for this model.`
                      : "Unavailable for this model."}
                  </span>
                </div>
              )}
              <Slider
                label="Pause between sentences" value={pause} min={100} max={900} step={50}
                fmt={(v) => `${v}ms`} hint="Silence inserted between assistant sentences."
                onChange={(v) => { setPause(v); setApplied(false); }}
              />
              <Slider
                label="Empathy" value={empathy} min={0} max={100} step={5}
                fmt={(v) => `${v}%`}
                hint="Shapes live LLM replies — cannot rewrite this fixed sample text."
                onChange={(v) => { setEmpathy(v); setApplied(false); }}
              />
              <Slider
                label="Energy" value={energy} min={0} max={100} step={5}
                fmt={(v) => `${v}%`}
                hint="Native voice mapping where the provider supports it."
                onChange={(v) => { setEnergy(v); setApplied(false); }}
              />
            </div>
          </div>
        </div>

        {/* 4 — Pronunciation (models that document a dictionary control) */}
        {widgetEntries.length > 0 && (
          <div className="preview-settings">
            <span className="t-label">Pronunciation</span>
            <div className="col gap-12" style={{ marginTop: 10 }}>
              {widgetEntries.map(([key, spec]) => (
                <DictionaryField
                  key={`preview-dict:${form.provider}:${form.model}:${key}`}
                  value={typeof draft[key] === "string" ? (draft[key] as string) : null}
                  help={spec.help}
                  onChange={(dictId) => {
                    setDraft((d) => {
                      const next = { ...d };
                      if (dictId) next[key] = dictId;
                      else delete next[key];
                      return next;
                    });
                    setApplied(false);
                  }}
                />
              ))}
            </div>
          </div>
        )}

        {/* 5 — Sample text LAST: finish tuning, then write and preview. */}
        <Field
          label="Sample text"
          hint={`${textLen}/500 characters`}
          error={textLen > 500 ? "Sample text must be 500 characters or fewer." : undefined}
        >
          <textarea
            className="textarea" rows={3} value={form.text} maxLength={600}
            aria-label="Preview sample text"
            onChange={(e) => setForm((f) => ({ ...f, text: e.target.value }))}
          />
        </Field>
        <p className="t-micro" style={{ margin: 0 }}>
          Generate synthesizes this text with the draft settings above — the same
          delivery mapping live calls use. Sample text is never saved; the settings
          are staged with Apply and persisted only by &ldquo;Save voice settings&rdquo;.
        </p>
        {error && <Callout tone="critical" title="Preview failed">{error}</Callout>}
        {result && (
          <div className="row gap-6 t-micro" style={{ alignItems: "center" }}>
            <Icon name={playing ? "volume" : "check-circle"} size={13} />
            <span className="t-num">
              Time to first audio: {Math.round(result.ttfaMs)} ms · Total: {Math.round(result.totalMs)} ms · {result.provider}
            </span>
          </div>
        )}
      </div>
    </Modal>
  );
}
