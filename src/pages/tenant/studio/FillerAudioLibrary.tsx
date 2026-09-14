/* Natural Conversation — filler audio library, preview and selection.

   The voice runtime covers the gap before a reply with a short pre-rendered
   sound (breath / inhale / exhale / inhale-exhale) matched to the active
   voice's gender, and on long waits with voiced cues rendered in the bot's
   own voice. This section lists every sound the runtime could play, streams
   the exact audio asset for preview, and lets the operator choose which kind
   covers the gap and which clips of it may rotate (primary + alternates).
   Selections live in the same sparse `humanSpeech` override as every other
   naturalness setting — no second source of truth. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button, Callout } from "@/components/ui";
import {
  getNaturalConversationAudio,
  naturalConversationClipUrl,
  naturalConversationCueUrl,
} from "@/services/api";
import { getToken } from "@/services/http";
import type {
  AudioChoice,
  CueSelection,
  FillerAudioClip,
  FillerAudioSelection,
  FillerGender,
  FillerSoundKind,
  HumanSpeechEffectiveSettings,
  HumanSpeechSettings,
  NaturalConversationAudio,
} from "@/types/domain";

interface Props {
  botId: string;
  /** Sparse bot overrides (the object the Save button sends). */
  override: HumanSpeechSettings;
  /** Effective values when the bot does not override (tenant/platform). */
  inherited: HumanSpeechEffectiveSettings;
  disabled?: boolean;
  onChange: (next: HumanSpeechSettings) => void;
}

const GENDER_LABEL: Record<FillerGender, string> = { male: "Male", female: "Female", neutral: "Neutral" };
const KIND_HELP: Record<FillerSoundKind, string> = {
  breath: "A soft breath that trails off into the wait. The default gap sound.",
  inhale: "A short breath drawn in, rising as if about to speak. Also the sound used before a long sentence inside a reply.",
  exhale: "A quick, settling breath out with a soft tail.",
  inhale_exhale: "A full quiet breath cycle: in, a brief hold, out.",
};

/** Authenticated fetch of a WAV: <audio src> cannot carry the JWT, so the
 *  bytes are fetched with the token and played from an object URL. */
export async function fetchAudioObjectUrl(url: string): Promise<string> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const resp = await fetch(url, { headers });
  if (!resp.ok) {
    let detail = "";
    try {
      const body = await resp.json();
      detail = body?.error?.message || body?.message || "";
    } catch {
      /* not JSON */
    }
    throw new Error(detail || `Could not load the audio (HTTP ${resp.status}).`);
  }
  const contentType = resp.headers.get("content-type")?.toLowerCase() ?? "";
  if (contentType.includes("json")) throw new Error("The server did not return audio.");
  return URL.createObjectURL(await resp.blob());
}

export function choiceIds(choice?: AudioChoice | null): string[] {
  if (!choice) return [];
  const ids: string[] = [];
  if (choice.primary) ids.push(choice.primary);
  for (const id of choice.alternates ?? []) if (id && !ids.includes(id)) ids.push(id);
  return ids;
}

function isEmptyChoice(choice?: AudioChoice | null): boolean {
  return !choice || (!choice.primary && !(choice.alternates ?? []).length);
}

/** Immutable update of one {kind, gender} choice; empty branches are pruned. */
export function withClipChoice(
  selection: FillerAudioSelection, kind: FillerSoundKind, gender: FillerGender, choice: AudioChoice | null,
): FillerAudioSelection {
  const next: FillerAudioSelection = { ...selection, [kind]: { ...(selection[kind] ?? {}) } };
  const byGender = next[kind] as Partial<Record<FillerGender, AudioChoice>>;
  if (isEmptyChoice(choice)) delete byGender[gender];
  else byGender[gender] = { primary: choice!.primary, alternates: [...(choice!.alternates ?? [])] };
  if (!Object.keys(byGender).length) delete next[kind];
  return next;
}

function formatDuration(ms: number): string {
  return `${(ms / 1000).toFixed(1)} s`;
}

export default function FillerAudioLibrary({ botId, override, inherited, disabled = false, onChange }: Props) {
  const [catalog, setCatalog] = useState<NaturalConversationAudio | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [genderFilter, setGenderFilter] = useState<"auto" | FillerGender>("auto");
  const [nowPlaying, setNowPlaying] = useState<{ key: string; label: string } | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [playError, setPlayError] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const objectUrlRef = useRef<string | null>(null);
  const playSeq = useRef(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    void getNaturalConversationAudio(botId).then((data) => {
      if (cancelled) return;
      setCatalog(data);
      setLoading(false);
    }).catch((error: unknown) => {
      if (cancelled) return;
      setLoadError(error instanceof Error ? error.message : "Could not load the filler audio library.");
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [botId, retry]);

  const stop = useCallback(() => {
    playSeq.current += 1;
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    }
    if (objectUrlRef.current) {
      URL.revokeObjectURL(objectUrlRef.current);
      objectUrlRef.current = null;
    }
    setNowPlaying(null);
    setBusyKey(null);
  }, []);

  useEffect(() => () => { stop(); }, [stop]);

  const play = useCallback(async (key: string, label: string, url: string) => {
    stop();
    const seq = playSeq.current;
    setPlayError(null);
    setBusyKey(key);
    try {
      const src = await fetchAudioObjectUrl(url);
      if (seq !== playSeq.current) {
        URL.revokeObjectURL(src);
        return;
      }
      objectUrlRef.current = src;
      const audio = audioRef.current ?? new Audio();
      audioRef.current = audio;
      audio.onended = () => { if (seq === playSeq.current) setNowPlaying(null); };
      audio.src = src;
      await audio.play();
      if (seq !== playSeq.current) return;
      setNowPlaying({ key, label });
    } catch (error) {
      if (seq !== playSeq.current) return;
      setPlayError(error instanceof Error ? error.message : "Could not play the audio.");
    } finally {
      if (seq === playSeq.current) setBusyKey(null);
    }
  }, [stop]);

  /* ---- effective values (override wins, else inherited) ---- */
  const kind: FillerSoundKind = override.latency_filler_kind ?? inherited.latency_filler_kind ?? "breath";
  const kindOverridden = Object.prototype.hasOwnProperty.call(override, "latency_filler_kind");
  const selection: FillerAudioSelection = override.filler_audio_selection ?? inherited.filler_audio_selection ?? {};
  const selectionOverridden = Object.prototype.hasOwnProperty.call(override, "filler_audio_selection");
  const cueSelection: CueSelection = override.latency_filler_cue_selection ?? inherited.latency_filler_cue_selection ?? {};
  const cueOverridden = Object.prototype.hasOwnProperty.call(override, "latency_filler_cue_selection");

  const setKind = (next: FillerSoundKind) => onChange({ ...override, latency_filler_kind: next });
  const inheritKind = () => {
    const next = { ...override };
    delete next.latency_filler_kind;
    onChange(next);
  };
  const updateSelection = (next: FillerAudioSelection) => {
    const out = { ...override };
    if (Object.keys(next).length) out.filler_audio_selection = next;
    else if (selectionOverridden) out.filler_audio_selection = {};
    else delete out.filler_audio_selection;
    onChange(out);
  };
  const inheritSelection = () => {
    const next = { ...override };
    delete next.filler_audio_selection;
    onChange(next);
  };
  const updateCueSelection = (next: CueSelection) => {
    const out = { ...override };
    if (Object.keys(next).length) out.latency_filler_cue_selection = next;
    else if (cueOverridden) out.latency_filler_cue_selection = {};
    else delete out.latency_filler_cue_selection;
    onChange(out);
  };
  const inheritCues = () => {
    const next = { ...override };
    delete next.latency_filler_cue_selection;
    onChange(next);
  };

  const runtimeGenders = useMemo(() => {
    const seen: FillerGender[] = [];
    for (const voice of catalog?.voices ?? []) if (!seen.includes(voice.gender)) seen.push(voice.gender);
    return seen.length ? seen : (["neutral"] as FillerGender[]);
  }, [catalog]);
  const visibleGenders: FillerGender[] = genderFilter === "auto" ? runtimeGenders : [genderFilter];

  if (loading) return <section className="card card-pad"><span className="t-micro">Loading filler audio library…</span></section>;
  if (loadError || !catalog) {
    return (
      <section className="card card-pad col gap-8">
        <span className="card-title">Filler audio library</span>
        <Callout tone="critical">{loadError ?? "Could not load the filler audio library."}</Callout>
        <div><Button size="sm" icon="refresh" onClick={() => setRetry((n) => n + 1)}>Retry</Button></div>
      </section>
    );
  }

  const choiceFor = (k: FillerSoundKind, gender: FillerGender): AudioChoice | null => selection[k]?.[gender] ?? null;

  const clipRow = (clip: FillerAudioClip, choice: AudioChoice | null, kindLabel: string) => {
    const key = `clip:${clip.id}`;
    const isPrimary = choice?.primary === clip.id;
    const isAlternate = !!choice?.alternates?.includes(clip.id);
    const label = `${kindLabel} · ${GENDER_LABEL[clip.gender]} · ${clip.label}`;
    return (
      <div key={clip.id} className="row gap-10" data-testid={`clip-row-${clip.id}`}
        style={{ flexWrap: "wrap", alignItems: "center", padding: "6px 0", borderTop: "1px solid var(--hairline)" }}>
        <Button size="sm" icon={nowPlaying?.key === key ? "square" : "play"} busy={busyKey === key}
          aria-label={`${nowPlaying?.key === key ? "Stop" : "Play"} ${label}`}
          onClick={() => (nowPlaying?.key === key ? stop() : void play(key, label, naturalConversationClipUrl(botId, clip.id)))}>
          {nowPlaying?.key === key ? "Stop" : "Play"}
        </Button>
        <span className="t-strong" style={{ minWidth: 140 }}>{clip.label}</span>
        <span className="t-micro">{clip.source === "recording" ? "Recording" : "Synthesized"} · {formatDuration(clip.durationMs)}</span>
        <label className="row gap-6 t-micro" style={{ marginLeft: "auto" }}>
          <input type="radio" name={`primary-${clip.kind}-${clip.gender}`} checked={isPrimary} disabled={disabled}
            aria-label={`Primary: ${label}`}
            onChange={() => updateSelection(withClipChoice(selection, clip.kind, clip.gender, {
              primary: clip.id, alternates: (choice?.alternates ?? []).filter((id) => id !== clip.id),
            }))} />
          Primary
        </label>
        <label className="row gap-6 t-micro">
          <input type="checkbox" checked={isAlternate} disabled={disabled || isPrimary}
            aria-label={`Alternate: ${label}`}
            onChange={(event) => {
              const alternates = (choice?.alternates ?? []).filter((id) => id !== clip.id);
              if (event.target.checked) alternates.push(clip.id);
              updateSelection(withClipChoice(selection, clip.kind, clip.gender, { primary: choice?.primary, alternates }));
            }} />
          Alternate
        </label>
      </div>
    );
  };

  const rotationSummary = (clips: FillerAudioClip[], choice: AudioChoice | null) => {
    const ids = choiceIds(choice).filter((id) => clips.some((c) => c.id === id));
    if (!ids.length) return `No selection — all ${clips.length} clip${clips.length === 1 ? "" : "s"} rotate.`;
    const names = ids.map((id) => clips.find((c) => c.id === id)?.label ?? id);
    return ids.length === 1 ? `Always plays: ${names[0]}.` : `Rotation: ${names.join(" → ")} (primary first, never the same twice in a row).`;
  };

  return (
    <section className="card card-pad col gap-14" data-testid="filler-audio-library">
      <div className="row-between gap-12" style={{ flexWrap: "wrap" }}>
        <div>
          <span className="card-title">Filler audio library &amp; preview</span>
          <p className="t-sub" style={{ margin: "4px 0 0" }}>
            Listen to every sound the runtime can play in the gap before a reply, then choose the primary and alternate clips per sound.
            Previews stream the exact audio asset the runtime uses.
          </p>
        </div>
        <div className="col gap-4 t-micro" style={{ textAlign: "right" }}>
          {catalog.voices.map((voice) => (
            <span key={voice.language}>
              {voice.language}: {voice.voiceName || voice.voice || "default voice"} ({GENDER_LABEL[voice.gender].toLowerCase()})
            </span>
          ))}
        </div>
      </div>

      <Callout tone="info">
        At runtime only clips matching the active voice&apos;s gender are eligible
        ({runtimeGenders.map((g) => GENDER_LABEL[g].toLowerCase()).join(", ")} for this bot).
        A male voice never plays a female breath, whatever is selected here. Use the filter to inspect the other library.
      </Callout>

      <div className="row gap-8" style={{ flexWrap: "wrap", alignItems: "center" }} role="group" aria-label="Audio gender filter">
        <span className="t-micro">Show:</span>
        <Button size="sm" variant={genderFilter === "auto" ? "primary" : "secondary"} onClick={() => setGenderFilter("auto")}>
          Match bot voice
        </Button>
        {catalog.genders.map((gender) => (
          <Button key={gender} size="sm" variant={genderFilter === gender ? "primary" : "secondary"}
            onClick={() => setGenderFilter(gender)}>
            {GENDER_LABEL[gender]}
          </Button>
        ))}
      </div>

      <div className="row gap-10" role="status" aria-live="polite" data-testid="now-playing" style={{ alignItems: "center", minHeight: 28 }}>
        {nowPlaying
          ? <><span className="t-strong">Now playing:</span>{" "}<span>{nowPlaying.label}</span>{" "}<Button size="sm" variant="ghost" onClick={stop}>Stop</Button></>
          : <span className="t-micro">Nothing playing.</span>}
        {playError && <span className="t-micro" style={{ color: "var(--status-critical)" }}>{playError}</span>}
      </div>

      <div className="col gap-8" data-testid="latency-filler-kind">
        <div className="row-between gap-8">
          <span className="t-strong">Sound before the reply</span>
          {kindOverridden && <Button size="sm" variant="ghost" disabled={disabled} onClick={inheritKind}>Inherit</Button>}
        </div>
        <p className="t-micro" style={{ margin: 0 }}>
          Plays once the reply has not started speaking after the configured delay, and stops the instant the reply begins.
          The inhale is also used before long sentences inside a reply.
        </p>
        <div className="row gap-12" style={{ flexWrap: "wrap" }}>
          {catalog.kinds.map((option) => (
            <label key={option.id} className="row gap-6">
              <input type="radio" name="latency-filler-kind" value={option.id} checked={kind === option.id} disabled={disabled}
                aria-label={`Gap sound: ${option.label}`} onChange={() => setKind(option.id)} />
              {option.label}
            </label>
          ))}
        </div>
      </div>

      {catalog.kinds.map((option) => (
        <div key={option.id} className="col gap-6" data-testid={`filler-kind-${option.id}`}
          style={{ border: "1px solid var(--hairline)", borderRadius: 10, padding: 12 }}>
          <div className="row-between gap-8" style={{ flexWrap: "wrap" }}>
            <div className="row gap-8" style={{ alignItems: "center" }}>
              <span className="t-strong">{option.label}</span>
              {kind === option.id && <span className="t-micro" style={{ color: "var(--status-good)" }}>· plays before the reply</span>}
              {option.id === "inhale" && <span className="t-micro">· also inside replies</span>}
            </div>
          </div>
          <p className="t-micro" style={{ margin: 0 }}>{KIND_HELP[option.id]}</p>
          {visibleGenders.map((gender) => {
            const clips = catalog.clips[option.id]?.[gender] ?? [];
            const choice = choiceFor(option.id, gender);
            return (
              <div key={gender} className="col gap-2" data-testid={`clips-${option.id}-${gender}`}>
                <div className="row-between gap-8" style={{ marginTop: 6 }}>
                  <span className="t-micro t-strong">{GENDER_LABEL[gender]} voices · {clips.length} clip{clips.length === 1 ? "" : "s"}</span>
                  {!isEmptyChoice(choice) && (
                    <Button size="sm" variant="ghost" disabled={disabled}
                      onClick={() => updateSelection(withClipChoice(selection, option.id, gender, null))}>
                      Use all clips
                    </Button>
                  )}
                </div>
                {clips.length === 0 && <span className="t-micro">No clip available.</span>}
                {clips.map((clip) => clipRow(clip, choice, option.label))}
                <span className="t-micro" data-testid={`rotation-${option.id}-${gender}`}>{rotationSummary(clips, choice)}</span>
              </div>
            );
          })}
        </div>
      ))}

      {selectionOverridden && (
        <div className="row gap-8" style={{ alignItems: "center" }}>
          <span className="t-micro">Clip selection is set for this bot.</span>
          <Button size="sm" variant="ghost" disabled={disabled} onClick={inheritSelection}>Inherit tenant/platform selection</Button>
        </div>
      )}

      <div className="col gap-8" data-testid="voiced-cues">
        <div className="row-between gap-8">
          <span className="t-strong">Voiced cues on long waits</span>
          {cueOverridden && <Button size="sm" variant="ghost" disabled={disabled} onClick={inheritCues}>Inherit</Button>}
        </div>
        <p className="t-micro" style={{ margin: 0 }}>
          When the reply still has not started after the breath, a short cue in the bot&apos;s own voice may follow, then a spoken wait cue.
          Tick the cues this bot is allowed to use; the runtime decides per turn whether a word is needed at all and which allowed cue fits
          what the caller just said (thinking, information given, confirmation, agreement, courtesy, concern). Never a fixed sequence, never the
          same cue twice in a row. The neutral default is used when nothing more specific fits. Previews are rendered once with the bot&apos;s voice
          and reused by live calls.
        </p>
        {Object.keys(catalog.cues).length === 0 && <span className="t-micro">No voiced cues for this bot&apos;s languages.</span>}
        {Object.entries(catalog.cues).map(([base, entry]) => {
          const choice = cueSelection[base] ?? entry.defaultSelection ?? null;
          const overriddenHere = !!cueSelection[base];
          return (
            <div key={base} className="col gap-4" style={{ border: "1px solid var(--hairline)", borderRadius: 10, padding: 12 }}>
              <div className="row-between gap-8">
                <span className="t-strong">{entry.language}</span>
                {overriddenHere && (
                  <Button size="sm" variant="ghost" disabled={disabled} onClick={() => {
                    const next = { ...cueSelection };
                    delete next[base];
                    updateCueSelection(next);
                  }}>Language default</Button>
                )}
              </div>
              {catalog.cueKinds.map((cueKind) => (
                <div key={cueKind.id} className="col gap-2">
                  <span className="t-micro t-strong" style={{ marginTop: 6 }}>{cueKind.label}</span>
                  {(entry.options[cueKind.id] ?? []).map((cue) => {
                    const key = `cue:${base}:${cueKind.id}:${cue.id}`;
                    const label = `${cueKind.label} · ${entry.language} · ${cue.text}`;
                    const isPrimary = cueKind.id === "hmm" && choice?.primary === cue.id;
                    const isAlternate = cueKind.id === "hmm" && !!choice?.alternates?.includes(cue.id);
                    return (
                      <div key={cue.id} className="row gap-10" data-testid={`cue-row-${base}-${cueKind.id}-${cue.id}`}
                        style={{ flexWrap: "wrap", alignItems: "center", padding: "6px 0", borderTop: "1px solid var(--hairline)" }}>
                        <Button size="sm" icon={nowPlaying?.key === key ? "square" : "play"} busy={busyKey === key}
                          aria-label={`${nowPlaying?.key === key ? "Stop" : "Play"} ${label}`}
                          onClick={() => (nowPlaying?.key === key ? stop()
                            : void play(key, label, naturalConversationCueUrl(botId, entry.language, cueKind.id, cue.id)))}>
                          {nowPlaying?.key === key ? "Stop" : "Play"}
                        </Button>
                        <span className="t-strong" lang={base} style={{ minWidth: 120 }}>{cue.text}</span>
                        <span className="t-micro">{cue.ready ? "Rendered" : "Renders on first play"}</span>
                        {cueKind.id === "hmm" && (
                          <>
                            <label className="row gap-6 t-micro" style={{ marginLeft: "auto" }}>
                              <input type="radio" name={`cue-primary-${base}`} checked={isPrimary} disabled={disabled}
                                aria-label={`Neutral default: ${label}`}
                                onChange={() => updateCueSelection({
                                  ...cueSelection,
                                  [base]: { primary: cue.id, alternates: (choice?.alternates ?? []).filter((id) => id !== cue.id) },
                                })} />
                              Neutral default
                            </label>
                            <label className="row gap-6 t-micro">
                              <input type="checkbox" checked={isAlternate} disabled={disabled || isPrimary}
                                aria-label={`Allowed: ${label}`}
                                onChange={(event) => {
                                  const alternates = (choice?.alternates ?? []).filter((id) => id !== cue.id);
                                  if (event.target.checked) alternates.push(cue.id);
                                  updateCueSelection({ ...cueSelection, [base]: { primary: choice?.primary, alternates } });
                                }} />
                              Allowed
                            </label>
                          </>
                        )}
                      </div>
                    );
                  })}
                  {cueKind.id === "hmm" && (
                    <span className="t-micro" data-testid={`cue-rotation-${base}`}>
                      {(() => {
                        const options = entry.options.hmm ?? [];
                        const ids = choiceIds(choice).filter((id) => options.some((o) => o.id === id));
                        const texts = ids.map((id) => options.find((o) => o.id === id)?.text ?? id);
                        if (!texts.length) return "Language default.";
                        return texts.length === 1
                          ? `Only cue in use: ${texts[0]} (when a word is needed at all).`
                          : `Chosen by context among: ${texts.join(", ")}.`;
                      })()}
                    </span>
                  )}
                </div>
              ))}
            </div>
          );
        })}
      </div>
    </section>
  );
}
