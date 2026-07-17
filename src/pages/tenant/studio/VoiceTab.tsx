import { useEffect, useMemo, useState } from "react";
import type { VoiceBot, VoiceProfile, VoiceTuning } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import { getVoiceCatalog, getVoiceSettings, listVoices, saveVoiceSettings } from "@/services/api";
import { Button, CardSkeleton, EmptyState, StatusChip } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";
import { flags } from "@/services/flags";

type EngineOverrides = {
  sttProvider: string; sttModel: string;
  ttsProvider: string; ttsModel: string; ttsVoice: string;
  llmProvider: string; llmModel: string;
};

const emptyEngines: EngineOverrides = {
  sttProvider: "", sttModel: "", ttsProvider: "", ttsModel: "", ttsVoice: "", llmProvider: "", llmModel: "",
};

export default function VoiceTab({ bot }: { bot: VoiceBot }) {
  const q = useAsync(listVoices, []);
  const settingsQ = useAsync(() => getVoiceSettings(bot.id), [bot.id]);
  const catalogQ = useAsync(getVoiceCatalog, []);
  const { toast } = useApp();
  const [selected, setSelected] = useState(bot.voiceId ?? "");
  const [query, setQuery] = useState("");
  const [langFilter, setLangFilter] = useState("all");
  const [genderFilter, setGenderFilter] = useState("all");
  const [saving, setSaving] = useState(false);
  const [tuning, setTuning] = useState<VoiceTuning>({ speed: 1, pauseMs: 350, empathy: 50, energy: 50 });
  const [langMap, setLangMap] = useState<Record<string, string>>({});
  const [engines, setEngines] = useState<EngineOverrides>(emptyEngines);

  /* Initialize tuning + mapping from persisted settings once loaded */
  useEffect(() => {
    const s = settingsQ.data;
    if (!s) return;
    setTuning({ speed: s.speed, pauseMs: s.pauseMs, empathy: s.empathy, energy: s.energy });
    if (s.voiceId) setSelected(s.voiceId);
    setLangMap((prev) => {
      const next: Record<string, string> = {};
      for (const l of bot.languages) next[l] = s.languageVoiceMap[l] ?? prev[l] ?? s.voiceId ?? "";
      return next;
    });
    setEngines({
      sttProvider: s.sttProvider ?? "", sttModel: s.sttModel ?? "",
      ttsProvider: s.ttsProvider ?? "", ttsModel: s.ttsModel ?? "", ttsVoice: s.ttsVoice ?? "",
      llmProvider: s.llmProvider ?? "", llmModel: s.llmModel ?? "",
    });
  }, [settingsQ.data, bot.languages]);

  const voices = useMemo(() => {
    let v = q.data ?? [];
    if (query) {
      const s = query.toLowerCase();
      v = v.filter((x) => x.name.toLowerCase().includes(s) || x.accent.toLowerCase().includes(s) || x.styles.some((st) => st.toLowerCase().includes(s)));
    }
    if (langFilter !== "all") v = v.filter((x) => x.languages.includes(langFilter));
    if (genderFilter !== "all") v = v.filter((x) => x.gender === genderFilter);
    return v;
  }, [q.data, query, langFilter, genderFilter]);

  const save = async () => {
    setSaving(true);
    try {
      await saveVoiceSettings(bot.id, {
        voiceId: selected || null,
        speed: tuning.speed,
        pauseMs: tuning.pauseMs,
        empathy: tuning.empathy,
        energy: tuning.energy,
        languageVoiceMap: langMap,
        /* Empty string = "Platform default" → persist as null */
        sttProvider: engines.sttProvider || null,
        sttModel: engines.sttModel.trim() || null,
        ttsProvider: engines.ttsProvider || null,
        ttsModel: engines.ttsModel.trim() || null,
        ttsVoice: engines.ttsVoice.trim() || null,
        llmProvider: engines.llmProvider || null,
        llmModel: engines.llmModel.trim() || null,
      });
      toast("Voice settings saved to draft — publish to make them live");
      settingsQ.reload();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Could not save voice settings", "error");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="grid" style={{ gridTemplateColumns: "1.7fr 1fr", gap: 20 }}>
      <div className="col gap-16">
        <div className="filter-bar" style={{ marginBottom: 0 }}>
          <div className="search-box">
            <Icon name="search" size={14} />
            <input className="input" placeholder="Search voices, accents, styles…" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search voices" />
          </div>
          <select className="select" value={langFilter} onChange={(e) => setLangFilter(e.target.value)} aria-label="Filter by language">
            <option value="all">All languages</option>
            {["en-US", "es-US", "en-GB", "fr-FR", "hi-IN", "vi-VN"].map((l) => <option key={l}>{l}</option>)}
          </select>
          <select className="select" value={genderFilter} onChange={(e) => setGenderFilter(e.target.value)} aria-label="Filter by gender">
            <option value="all">Any gender</option>
            <option value="female">Female</option>
            <option value="male">Male</option>
            <option value="neutral">Neutral</option>
          </select>
        </div>

        {q.loading && <div className="grid grid-2">{Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} rows={3} />)}</div>}
        {q.error && <EmptyState icon="alert" title="Couldn’t load voices" body={q.error} action={<Button icon="refresh" onClick={q.reload}>Retry</Button>} />}
        {!q.loading && voices.length === 0 && <EmptyState icon="volume" title="No voices match" body="Loosen the language or gender filters." />}

        <div className="grid grid-2">
          {voices.map((v) => (
            <VoiceCard key={v.id} voice={v} selected={selected === v.id} onSelect={() => setSelected(v.id)} />
          ))}
        </div>
      </div>

      <div className="col gap-16">
        {/* Tuning */}
        <div className="card">
          <div className="card-header"><span className="card-title">Delivery tuning</span></div>
          <div className="col" style={{ padding: 18, gap: 16 }}>
            <Slider label="Speaking speed" value={tuning.speed} min={0.7} max={1.4} step={0.05} fmt={(v) => `${v.toFixed(2)}×`}
              onChange={(v) => setTuning((t) => ({ ...t, speed: v }))} />
            <Slider label="Pause between sentences" value={tuning.pauseMs} min={100} max={900} step={50} fmt={(v) => `${v}ms`}
              onChange={(v) => setTuning((t) => ({ ...t, pauseMs: v }))} />
            <Slider label="Empathy" value={tuning.empathy} min={0} max={100} step={5} fmt={(v) => `${v}%`}
              onChange={(v) => setTuning((t) => ({ ...t, empathy: v }))} />
            <Slider label="Energy" value={tuning.energy} min={0} max={100} step={5} fmt={(v) => `${v}%`}
              onChange={(v) => setTuning((t) => ({ ...t, energy: v }))} />
            <Button
              icon="play"
              disabled={!flags.voiceSamplePlayback}
              title={flags.voiceSamplePlayback ? undefined : "Sample synthesis pending backend (TODO_BACKEND #2)"}
            >
              Preview with current tuning
            </Button>
          </div>
        </div>

        {/* Runtime engine providers */}
        <div className="card">
          <div className="card-header">
            <div className="col gap-2">
              <span className="card-title">Speech &amp; intelligence providers</span>
              <span className="t-micro">Runtime engines for this bot — leave on platform default unless you need an override</span>
            </div>
          </div>
          <div className="col" style={{ padding: 18, gap: 14 }}>
            {catalogQ.error && <span className="t-micro" style={{ color: "var(--status-critical)" }}>{catalogQ.error}</span>}
            <EngineRow
              label="Speech-to-text (STT)"
              providers={catalogQ.data?.providers.stt ?? []}
              defaults={catalogQ.data?.defaults.stt}
              provider={engines.sttProvider} model={engines.sttModel}
              onProvider={(v) => setEngines((e) => ({ ...e, sttProvider: v }))}
              onModel={(v) => setEngines((e) => ({ ...e, sttModel: v }))}
            />
            <EngineRow
              label="Text-to-speech (TTS)"
              providers={catalogQ.data?.providers.tts ?? []}
              defaults={catalogQ.data?.defaults.tts}
              provider={engines.ttsProvider} model={engines.ttsModel} voice={engines.ttsVoice}
              onProvider={(v) => setEngines((e) => ({ ...e, ttsProvider: v }))}
              onModel={(v) => setEngines((e) => ({ ...e, ttsModel: v }))}
              onVoice={(v) => setEngines((e) => ({ ...e, ttsVoice: v }))}
            />
            <EngineRow
              label="Language model (LLM)"
              providers={catalogQ.data?.providers.llm ?? []}
              defaults={catalogQ.data?.defaults.llm}
              provider={engines.llmProvider} model={engines.llmModel}
              onProvider={(v) => setEngines((e) => ({ ...e, llmProvider: v }))}
              onModel={(v) => setEngines((e) => ({ ...e, llmModel: v }))}
            />
          </div>
        </div>

        {/* Language → voice mapping */}
        <div className="card">
          <div className="card-header">
            <div className="col gap-2">
              <span className="card-title">Language mapping</span>
              <span className="t-micro">Which voice answers in each language</span>
            </div>
          </div>
          <div className="col" style={{ padding: 18, gap: 12 }}>
            {bot.languages.map((l) => (
              <div key={l} className="row-between">
                <span className="tag">{l}</span>
                <select className="select" style={{ width: 170 }} value={langMap[l] ?? ""} aria-label={`Voice for ${l}`}
                  onChange={(e) => setLangMap((m) => ({ ...m, [l]: e.target.value }))}>
                  <option value="">Default voice</option>
                  {(q.data ?? []).filter((v) => v.languages.includes(l)).map((v) => <option key={v.id} value={v.id}>{v.name} — {v.accent.split("·")[0].trim()}</option>)}
                </select>
              </div>
            ))}
          </div>
        </div>

        <Button variant="primary" icon="check" onClick={save} disabled={saving || settingsQ.loading}>
          {saving ? "Saving…" : "Save voice settings"}
        </Button>
      </div>
    </div>
  );
}

function VoiceCard({ voice, selected, onSelect }: { voice: VoiceProfile; selected: boolean; onSelect: () => void }) {
  const { toast } = useApp();
  const [playing, setPlaying] = useState(false);
  const play = () => {
    if (!flags.voiceSamplePlayback) {
      setPlaying(true);
      setTimeout(() => setPlaying(false), 1800);
      toast("Sample playback simulated — real synthesis pending backend (TODO_BACKEND #2)", "info");
    }
  };
  return (
    <div
      className="card card-pad card-clickable col gap-10"
      style={selected ? { borderColor: "var(--brand-500)", boxShadow: "0 0 0 3px var(--brand-100), var(--shadow-1)" } : undefined}
      onClick={onSelect}
      role="radio"
      aria-checked={selected}
      tabIndex={0}
      onKeyDown={(e) => e.key === "Enter" && onSelect()}
    >
      <div className="row-between">
        <div className="row gap-10">
          <button
            className="btn-icon"
            style={{ background: playing ? "var(--brand-500)" : "var(--brand-100)", color: playing ? "#fff" : "var(--brand-600)", borderRadius: "50%" }}
            aria-label={`Play ${voice.name} sample`}
            onClick={(e) => { e.stopPropagation(); play(); }}
          >
            <Icon name={playing ? "pause" : "play"} size={13} />
          </button>
          <div>
            <span className="t-strong" style={{ fontSize: 14 }}>{voice.name}</span>
            <div className="t-micro">{voice.accent} · {voice.gender}</div>
          </div>
        </div>
        {selected ? <StatusChip status="approved" label="Selected" /> : voice.premium ? <span className="chip chip-brand"><Icon name="sparkles" size={11} />Premium</span> : null}
      </div>
      <p className="t-sub" style={{ fontSize: 12.5, fontStyle: "italic" }}>“{voice.sample}”</p>
      <div className="row-between t-micro">
        <span className="row gap-4 wrap">{voice.styles.map((s) => <span key={s} className="tag" style={{ height: 18, fontSize: 10.5 }}>{s}</span>)}</span>
        <span className="t-num">{voice.latencyMs}ms · {voice.languages.length} lang</span>
      </div>
    </div>
  );
}

function EngineRow({ label, providers, defaults, provider, model, voice, onProvider, onModel, onVoice }: {
  label: string;
  providers: string[];
  defaults?: { provider: string; model: string; voice?: string };
  provider: string;
  model: string;
  voice?: string;
  onProvider: (v: string) => void;
  onModel: (v: string) => void;
  onVoice?: (v: string) => void;
}) {
  return (
    <div className="col gap-6">
      <span className="field-label">{label}</span>
      <select className="select" value={provider} aria-label={`${label} provider`} onChange={(e) => onProvider(e.target.value)}>
        <option value="">Platform default{defaults ? ` (${defaults.provider})` : ""}</option>
        {providers.map((p) => <option key={p} value={p}>{p}</option>)}
      </select>
      <input
        className="input" value={model} aria-label={`${label} model`}
        placeholder={defaults ? `Model — default ${defaults.model}` : "Model"}
        onChange={(e) => onModel(e.target.value)}
      />
      {onVoice && (
        <input
          className="input" value={voice ?? ""} aria-label={`${label} voice`}
          placeholder={defaults?.voice ? `Voice — default ${defaults.voice}` : "Voice"}
          onChange={(e) => onVoice(e.target.value)}
        />
      )}
    </div>
  );
}

function Slider({ label, value, min, max, step, fmt, onChange }: {
  label: string; value: number; min: number; max: number; step: number;
  fmt: (v: number) => string; onChange: (v: number) => void;
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
    </label>
  );
}
