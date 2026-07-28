import { useEffect, useRef, useState } from "react";
import { useApp } from "@/state/AppContext";
import { useAsync } from "@/hooks/useAsync";
import {
  createVoiceClone,
  deleteVoiceClone,
  generateTtsPreview,
  getModelLanguages,
  getVoiceCloneConfig,
  listProviderModels,
  listVoiceClones,
  setVoiceCloneStatus,
  updateVoiceClone,
} from "@/services/api";
import type { ApiRequestError } from "@/services/http";
import type {
  VoiceCloneConfig,
  VoiceCloneParamSpec,
  VoiceCloneProviderInfo,
  VoiceProfile,
} from "@/types/domain";
import {
  Button,
  Callout,
  CardSkeleton,
  ConfirmModal,
  EmptyState,
  ErrorState,
  Field,
  Modal,
  StatusChip,
  Toggle,
} from "@/components/ui";
import { Icon } from "@/components/Icon";

const DEFAULT_SAMPLE_TEXT = "Hello! This is a preview of my cloned voice on EchoSphere.";

const fmtFileSize = (bytes: number) =>
  bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;

export default function Voices() {
  const { hasPermission } = useApp();
  const canManage = hasPermission("manage_voices") || hasPermission("bots.manage");
  const clonesQ = useAsync(listVoiceClones, []);
  const configQ = useAsync(getVoiceCloneConfig, []);

  const [cloneOpen, setCloneOpen] = useState(false);
  const [editVoice, setEditVoice] = useState<VoiceProfile | null>(null);
  const [previewVoice, setPreviewVoice] = useState<VoiceProfile | null>(null);
  const [deleteVoice, setDeleteVoice] = useState<VoiceProfile | null>(null);

  if (clonesQ.loading) {
    return (
      <>
        <PageHead />
        <div className="grid grid-2">{Array.from({ length: 2 }).map((_, i) => <CardSkeleton key={i} rows={4} />)}</div>
      </>
    );
  }
  if (clonesQ.error) {
    return (
      <>
        <PageHead />
        <ErrorState message={clonesQ.error} onRetry={clonesQ.reload} />
      </>
    );
  }

  const voices = clonesQ.data ?? [];
  const config = configQ.data ?? null;

  return (
    <>
      <PageHead
        action={
          canManage ? (
            <Button variant="primary" icon="wand" onClick={() => setCloneOpen(true)}>
              Clone Voice
            </Button>
          ) : undefined
        }
      />
      {voices.length === 0 ? (
        <div className="card">
          <EmptyState
            icon="mic"
            title="No cloned voices yet"
            body="Clone a voice from a short audio sample — uploaded or recorded right here — and use it in any of your voice bots. Cloning is available for providers that offer a voice-cloning API (currently ElevenLabs)."
          />
          {canManage && (
            <div className="row" style={{ justifyContent: "center", paddingBottom: 16 }}>
              <Button variant="primary" icon="wand" onClick={() => setCloneOpen(true)}>Clone Voice</Button>
            </div>
          )}
        </div>
      ) : (
        <div className="col gap-12">
          {voices.map((v) => (
            <VoiceRow
              key={v.id}
              voice={v}
              canManage={canManage}
              onPreview={() => setPreviewVoice(v)}
              onEdit={() => setEditVoice(v)}
              onDelete={() => setDeleteVoice(v)}
              onChanged={clonesQ.reload}
            />
          ))}
        </div>
      )}
      <p className="t-micro mt-12">
        Cloned voices are private to your workspace. Speech generated with a cloned voice is billed
        exactly like any other voice of the same provider and model (per character).
      </p>

      <CloneVoiceModal
        open={cloneOpen}
        config={config}
        configError={configQ.error}
        onClose={() => setCloneOpen(false)}
        onSaved={() => { setCloneOpen(false); clonesQ.reload(); }}
      />
      <EditVoiceModal
        voice={editVoice}
        onClose={() => setEditVoice(null)}
        onSaved={() => { setEditVoice(null); clonesQ.reload(); }}
      />
      <PreviewCloneModal voice={previewVoice} onClose={() => setPreviewVoice(null)} />
      <DeleteVoiceModal
        voice={deleteVoice}
        onClose={() => setDeleteVoice(null)}
        onDeleted={() => { setDeleteVoice(null); clonesQ.reload(); }}
      />
    </>
  );
}

function PageHead({ action }: { action?: React.ReactNode }) {
  return (
    <div className="page-head">
      <div className="page-head-titles">
        <h1 className="page-title">Voices</h1>
        <p className="page-sub">Clone custom voices and manage them for your voice bots</p>
      </div>
      {action}
    </div>
  );
}

/* ---------- voice row ---------- */

function VoiceRow({ voice, canManage, onPreview, onEdit, onDelete, onChanged }: {
  voice: VoiceProfile;
  canManage: boolean;
  onPreview: () => void;
  onEdit: () => void;
  onDelete: () => void;
  onChanged: () => void;
}) {
  const { toast } = useApp();
  const [busy, setBusy] = useState(false);
  const active = voice.status === "active";
  const samples = voice.cloneMetadata?.samples ?? [];

  const toggleStatus = async () => {
    setBusy(true);
    try {
      await setVoiceCloneStatus(voice.id, active ? "inactive" : "active");
      toast(active ? `'${voice.name}' deactivated` : `'${voice.name}' activated`, "good");
      onChanged();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card" style={{ padding: 16 }}>
      <div className="row gap-12" style={{ alignItems: "flex-start" }}>
        <span className="icon-tile neutral" style={{ width: 38, height: 38, flexShrink: 0 }}>
          <Icon name="mic" size={17} />
        </span>
        <div className="col gap-4" style={{ minWidth: 0, flex: 1 }}>
          <div className="row gap-8" style={{ flexWrap: "wrap" }}>
            <span className="t-strong">{voice.name}</span>
            <span className="tag">Cloned</span>
            <span className="tag">{voice.provider}</span>
            <StatusChip status={voice.status || "active"} />
          </div>
          {voice.description && <span className="t-body">{voice.description}</span>}
          <span className="t-micro">
            Voice ID <code>{voice.providerVoiceId}</code>
            {samples.length > 0 && <> · {samples.length} sample{samples.length > 1 ? "s" : ""}</>}
            {voice.usageCount ? <> · used by {voice.usageCount} bot config{voice.usageCount > 1 ? "s" : ""}</> : null}
          </span>
        </div>
        <span className="row gap-6" style={{ flexShrink: 0 }}>
          <Button size="sm" variant="ghost" icon="play" onClick={onPreview} disabled={!active}>
            Preview
          </Button>
          {canManage && (
            <>
              <Button size="sm" variant="ghost" icon="settings" onClick={onEdit}>Edit</Button>
              <Button size="sm" variant="ghost" icon={active ? "pause" : "play"} busy={busy} onClick={() => void toggleStatus()}>
                {active ? "Deactivate" : "Activate"}
              </Button>
              <Button size="sm" variant="danger-ghost" icon="trash" onClick={onDelete} aria-label={`Delete ${voice.name}`} />
            </>
          )}
        </span>
      </div>
    </div>
  );
}

/* ---------- clone modal ---------- */

interface SampleRow {
  id: number;
  file: File;
  error?: string;
}

function CloneVoiceModal({ open, config, configError, onClose, onSaved }: {
  open: boolean;
  config: VoiceCloneConfig | null;
  configError: string | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { toast } = useApp();
  const [provider, setProvider] = useState("");
  const [name, setName] = useState("");
  const [params, setParams] = useState<Record<string, string | boolean>>({});
  const [sourceMode, setSourceMode] = useState<"upload" | "record">("upload");
  const [files, setFiles] = useState<SampleRow[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const rowSeq = useRef(0);
  const fileRef = useRef<HTMLInputElement>(null);

  const providers = config?.providers ?? [];
  const cloneable = providers.filter((p) => p.supportsCloning);
  const selected: VoiceCloneProviderInfo | undefined = providers.find((p) => p.code === provider);

  useEffect(() => {
    if (!open) return;
    const first = (config?.providers ?? []).find((p) => p.supportsCloning);
    setProvider(first?.code ?? "");
    setName("");
    setParams(defaultParams(first?.cloneParams ?? []));
    setSourceMode("upload");
    setFiles([]);
    setDragActive(false);
    setSubmitting(false);
    setError("");
    setFieldErrors({});
  }, [open, config]);

  const pickProvider = (code: string) => {
    setProvider(code);
    const info = providers.find((p) => p.code === code);
    setParams(defaultParams(info?.cloneParams ?? []));
    setFieldErrors((fe) => ({ ...fe, provider: "" }));
  };

  const addFiles = (list: FileList | File[]) => {
    if (!config) return;
    const allowed = config.allowedExtensions;
    const next = Array.from(list).map((file): SampleRow => {
      const ext = (/\.([^.]+)$/.exec(file.name)?.[1] ?? "").toLowerCase();
      let err = "";
      if (!allowed.includes(ext)) {
        err = `Unsupported file type${ext ? ` (.${ext})` : ""} — allowed: ${allowed.map((a) => `.${a}`).join(", ")}`;
      } else if (file.size === 0) {
        err = "File is empty";
      } else if (file.size > config.maxFileMb * 1024 * 1024) {
        err = `File is larger than the ${config.maxFileMb} MB limit`;
      }
      return { id: ++rowSeq.current, file, error: err || undefined };
    });
    if (next.length) setFiles((rows) => [...rows, ...next].slice(0, config.maxFiles));
    setFieldErrors((fe) => ({ ...fe, files: "" }));
  };

  const validFiles = files.filter((f) => !f.error);
  const totalBytes = validFiles.reduce((sum, f) => sum + f.file.size, 0);
  const overTotal = config ? totalBytes > config.maxTotalMb * 1024 * 1024 : false;
  const canSubmit =
    !!selected?.supportsCloning && selected.hasCredentials && !!name.trim() &&
    validFiles.length > 0 && files.every((f) => !f.error) && !overTotal && !submitting;

  const submit = async () => {
    if (!selected) return;
    setSubmitting(true);
    setError("");
    setFieldErrors({});
    try {
      const form = new FormData();
      form.append("provider", selected.code);
      form.append("name", name.trim());
      for (const spec of selected.cloneParams) {
        const value = params[spec.name];
        if (spec.type === "boolean") form.append(spec.name, value ? "true" : "false");
        else if (typeof value === "string" && value.trim()) form.append(spec.name, value.trim());
      }
      for (const row of validFiles) form.append("files", row.file, row.file.name);
      const created = await createVoiceClone(form);
      toast(`Voice '${created.name}' cloned`, "good");
      onSaved();
    } catch (e) {
      const err = e as ApiRequestError;
      if (err.fieldErrors && Object.keys(err.fieldErrors).length) setFieldErrors(err.fieldErrors);
      setError(err.message || "Voice cloning failed.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Clone Voice"
      sub="Create a custom voice from uploaded or recorded audio — it becomes selectable in your voice bots"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={submitting}>Cancel</Button>
          <Button variant="primary" icon="wand" busy={submitting} disabled={!canSubmit} onClick={() => void submit()}>
            {submitting ? "Creating voice…" : "Clone Voice"}
          </Button>
        </>
      }
    >
      <div className="col gap-12">
        {configError && <Callout tone="critical" title="Could not load cloning settings">{configError}</Callout>}
        {!configError && config && cloneable.length === 0 && (
          <Callout tone="warning" title="No cloning-capable provider">
            None of the active TTS providers offers a voice-cloning API.
          </Callout>
        )}

        <Field label="Provider" required error={fieldErrors.provider || undefined}>
          <select
            className="select"
            value={provider}
            onChange={(e) => pickProvider(e.target.value)}
            aria-label="Clone provider"
            disabled={submitting}
          >
            {!provider && <option value="">Select a provider…</option>}
            {providers.map((p) => (
              <option key={p.code} value={p.code}>
                {p.name}{p.supportsCloning ? "" : " — cloning not available"}
              </option>
            ))}
          </select>
        </Field>

        {selected && !selected.supportsCloning && (
          <Callout tone="info" title={`${selected.name} does not support voice cloning`}>
            {selected.reason}
          </Callout>
        )}
        {selected?.supportsCloning && !selected.hasCredentials && (
          <Callout tone="warning" title="Credentials missing">
            No API key is configured for {selected.name} — voice cloning will fail until it is set.
          </Callout>
        )}

        {selected?.supportsCloning && (
          <>
            <Field label="Voice name" required error={fieldErrors.name || undefined}>
              <input
                className="input"
                value={name}
                maxLength={100}
                placeholder="e.g. Support narrator — Hindi"
                onChange={(e) => { setName(e.target.value); setFieldErrors((fe) => ({ ...fe, name: "" })); }}
                disabled={submitting}
              />
            </Field>

            {selected.cloneParams.map((spec) => (
              <CloneParamField
                key={spec.name}
                spec={spec}
                value={params[spec.name]}
                disabled={submitting}
                error={fieldErrors[spec.name] || undefined}
                onChange={(value) => setParams((p) => ({ ...p, [spec.name]: value }))}
              />
            ))}

            <Field
              label="Reference audio"
              required
              hint={config
                ? `Clear speech, no background music. ${config.allowedExtensions.map((e) => `.${e}`).join(", ")} · up to ${config.maxFiles} files, ${config.maxFileMb} MB each`
                : undefined}
              error={fieldErrors.files || undefined}
            >
              <div className="col gap-8">
                <div className="segmented" role="group" aria-label="Reference audio source">
                  <button
                    type="button"
                    aria-pressed={sourceMode === "upload"}
                    disabled={submitting}
                    onClick={() => setSourceMode("upload")}
                  >
                    Upload Audio File
                  </button>
                  <button
                    type="button"
                    aria-pressed={sourceMode === "record"}
                    disabled={submitting}
                    onClick={() => setSourceMode("record")}
                  >
                    Record Live Voice
                  </button>
                </div>
                {sourceMode === "upload" ? (
                  <div
                    className={`dropzone${dragActive ? " dropzone-active" : ""}`}
                    onDragEnter={(e) => { e.preventDefault(); if (config) setDragActive(true); }}
                    onDragOver={(e) => { e.preventDefault(); if (config) setDragActive(true); }}
                    onDragLeave={(e) => {
                      if (e.relatedTarget instanceof Node && e.currentTarget.contains(e.relatedTarget)) return;
                      setDragActive(false);
                    }}
                    onDrop={(e) => {
                      e.preventDefault();
                      setDragActive(false);
                      if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
                    }}
                  >
                    <span className="dropzone-icon"><Icon name="upload" size={20} /></span>
                    <span className="t-strong" style={{ fontSize: 13 }}>Drag and drop audio files here</span>
                    <span className="t-micro">or</span>
                    <Button icon="file" disabled={!config || submitting} onClick={() => fileRef.current?.click()}>
                      Choose files
                    </Button>
                    <input
                      ref={fileRef}
                      type="file"
                      multiple
                      accept={config?.accept}
                      style={{ display: "none" }}
                      aria-label="Choose audio samples"
                      onChange={(e) => { if (e.target.files?.length) addFiles(e.target.files); e.target.value = ""; }}
                    />
                  </div>
                ) : (
                  <LiveRecorder
                    disabled={submitting}
                    onUse={(file) => addFiles([file])}
                  />
                )}
              </div>
            </Field>

            {files.length > 0 && (
              <div className="col gap-8">
                {files.map((f) => (
                  <div key={f.id} className="file-row">
                    <span className="icon-tile neutral" style={{ width: 30, height: 30, flexShrink: 0 }}>
                      <Icon name="volume" size={14} />
                    </span>
                    <div className="file-row-main">
                      <div className="row gap-8" style={{ minWidth: 0 }}>
                        <span className="file-row-name" title={f.file.name}>{f.file.name}</span>
                        <span className="t-micro t-num" style={{ whiteSpace: "nowrap", flexShrink: 0 }}>
                          {fmtFileSize(f.file.size)}
                        </span>
                      </div>
                      {f.error && <span className="t-micro" style={{ color: "var(--status-critical)" }}>{f.error}</span>}
                    </div>
                    <span className="row gap-6" style={{ flexShrink: 0 }}>
                      <StatusChip status={f.error ? "error" : "available"} label={f.error ? "invalid" : "ready"} />
                      <Button
                        size="sm" variant="ghost" icon="x"
                        aria-label={`Remove ${f.file.name}`} title="Remove" disabled={submitting}
                        onClick={() => setFiles((rows) => rows.filter((r) => r.id !== f.id))}
                      />
                    </span>
                  </div>
                ))}
              </div>
            )}
            {overTotal && config && (
              <Callout tone="warning" title="Samples too large">
                Combined samples exceed the {config.maxTotalMb} MB limit — remove a file or use shorter recordings.
              </Callout>
            )}
            {submitting && (
              <Callout tone="info" title="Creating your voice">
                Uploading samples and waiting for the provider to build the clone — this can take a moment.
              </Callout>
            )}
          </>
        )}

        {error && <Callout tone="critical" title="Voice cloning failed">{error}</Callout>}
        <span className="t-micro">
          Only clone voices you have the rights to use. Samples are sent to the provider to build the
          voice and are not stored by EchoSphere.
        </span>
      </div>
    </Modal>
  );
}

function defaultParams(specs: VoiceCloneParamSpec[]): Record<string, string | boolean> {
  const out: Record<string, string | boolean> = {};
  for (const spec of specs) out[spec.name] = spec.type === "boolean" ? Boolean(spec.default) : String(spec.default ?? "");
  return out;
}

function CloneParamField({ spec, value, error, disabled, onChange }: {
  spec: VoiceCloneParamSpec;
  value: string | boolean | undefined;
  error?: string;
  disabled?: boolean;
  onChange: (value: string | boolean) => void;
}) {
  if (spec.type === "boolean") {
    return (
      <Field label={spec.label} hint={spec.help} error={error} plain>
        <Toggle checked={Boolean(value)} onChange={(checked) => { if (!disabled) onChange(checked); }} label={spec.label} />
      </Field>
    );
  }
  return (
    <Field label={spec.label} hint={spec.help} error={error}>
      <input
        className="input"
        value={typeof value === "string" ? value : ""}
        maxLength={spec.maxLength}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
      />
    </Field>
  );
}

/* ---------- live voice recorder ---------- */

const REC_MAX_SECONDS = 300;
const REC_MIME_CANDIDATES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/mp4",
  "audio/ogg;codecs=opus",
];

const recordingExtension = (mime: string): string => {
  if (mime.includes("mp4")) return "m4a";
  if (mime.includes("ogg")) return "ogg";
  if (mime.includes("mpeg")) return "mp3";
  if (mime.includes("wav")) return "wav";
  return "webm";
};

const micErrorMessage = (err: unknown): string => {
  const name = err instanceof DOMException ? err.name : "";
  if (name === "NotAllowedError" || name === "PermissionDeniedError") {
    return "Microphone access was denied — allow it for this site in your browser settings and try again.";
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError") {
    return "No microphone was found — connect one and try again.";
  }
  if (name === "NotReadableError" || name === "TrackStartError") {
    return "The microphone is already in use by another application.";
  }
  if (name === "SecurityError") {
    return "Recording requires a secure (HTTPS) connection.";
  }
  return "Could not start recording — check your microphone and try again.";
};

const fmtSeconds = (total: number) =>
  `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;

interface RecordedClip {
  blob: Blob;
  url: string;
  mime: string;
  seconds: number;
}

function LiveRecorder({ disabled, onUse }: { disabled?: boolean; onUse: (file: File) => void }) {
  const [phase, setPhase] = useState<"idle" | "requesting" | "recording" | "recorded">("idle");
  const [error, setError] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [clip, setClip] = useState<RecordedClip | null>(null);
  const [playing, setPlaying] = useState(false);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const timerRef = useRef<number | null>(null);
  const elapsedRef = useRef(0);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const stopTimer = () => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
  };

  const releaseStream = () => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  };

  const stopPreview = () => {
    audioRef.current?.pause();
    audioRef.current = null;
    setPlaying(false);
  };

  const discardClip = () => {
    stopPreview();
    if (clip) URL.revokeObjectURL(clip.url);
    setClip(null);
  };

  useEffect(() => () => {
    // Modal closed / mode switched mid-recording: stop everything, free the mic.
    stopTimer();
    try {
      if (recorderRef.current?.state === "recording") recorderRef.current.stop();
    } catch {
      /* recorder already stopped */
    }
    releaseStream();
    audioRef.current?.pause();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const startRecording = async () => {
    setError("");
    discardClip();
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setError("Recording is not supported in this browser — upload an audio file instead.");
      return;
    }
    setPhase("requesting");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      const mime = REC_MIME_CANDIDATES.find((m) => MediaRecorder.isTypeSupported?.(m));
      const recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
      chunksRef.current = [];
      recorder.ondataavailable = (e: BlobEvent) => {
        if (e.data && e.data.size > 0) chunksRef.current.push(e.data);
      };
      recorder.onstop = () => {
        stopTimer();
        releaseStream();
        const type = recorder.mimeType || chunksRef.current[0]?.type || "audio/webm";
        const blob = new Blob(chunksRef.current, { type });
        if (blob.size === 0) {
          setPhase("idle");
          setError("No audio was captured — check your microphone and try again.");
          return;
        }
        setClip({ blob, url: URL.createObjectURL(blob), mime: type, seconds: elapsedRef.current });
        setPhase("recorded");
      };
      recorder.start();
      recorderRef.current = recorder;
      elapsedRef.current = 0;
      setElapsed(0);
      setPhase("recording");
      timerRef.current = window.setInterval(() => {
        elapsedRef.current += 1;
        setElapsed(elapsedRef.current);
        if (elapsedRef.current >= REC_MAX_SECONDS) stopRecording();
      }, 1000);
    } catch (e) {
      releaseStream();
      setPhase("idle");
      setError(micErrorMessage(e));
    }
  };

  const stopRecording = () => {
    stopTimer();
    try {
      if (recorderRef.current?.state === "recording") recorderRef.current.stop();
    } catch {
      releaseStream();
      setPhase("idle");
      setError("Recording failed — please try again.");
    }
  };

  const togglePreview = async () => {
    if (!clip) return;
    if (playing) {
      stopPreview();
      return;
    }
    const audio = new Audio(clip.url);
    audioRef.current = audio;
    audio.onended = () => setPlaying(false);
    setPlaying(true);
    try {
      await audio.play();
    } catch {
      setPlaying(false);
      setError("Could not play the recording preview.");
    }
  };

  const useRecording = () => {
    if (!clip) return;
    const ext = recordingExtension(clip.mime);
    const file = new File([clip.blob], `recording-${Date.now()}.${ext}`, { type: clip.mime });
    onUse(file); // from here on it is treated exactly like an uploaded sample
    discardClip();
    setPhase("idle");
    setElapsed(0);
  };

  const reRecord = () => {
    discardClip();
    setPhase("idle");
    setElapsed(0);
    void startRecording();
  };

  return (
    <div className="dropzone" style={{ cursor: "default" }}>
      {phase === "idle" && (
        <>
          <span className="dropzone-icon"><Icon name="mic" size={20} /></span>
          <span className="t-strong" style={{ fontSize: 13 }}>Record your voice with the microphone</span>
          <span className="t-micro">Speak clearly for 30–60 seconds in a quiet room, no background music.</span>
          <Button icon="mic" disabled={disabled} onClick={() => void startRecording()}>
            Start Recording
          </Button>
        </>
      )}
      {phase === "requesting" && (
        <>
          <span className="dropzone-icon"><Icon name="mic" size={20} /></span>
          <span className="t-strong" style={{ fontSize: 13 }}>Waiting for microphone access…</span>
          <span className="t-micro">Allow microphone access in the browser prompt to start recording.</span>
        </>
      )}
      {phase === "recording" && (
        <>
          <span className="dropzone-icon" style={{ color: "var(--status-critical)" }}>
            <Icon name="mic" size={20} />
          </span>
          <span className="t-strong t-num" style={{ fontSize: 13 }} role="timer" aria-label="Recording time">
            Recording… {fmtSeconds(elapsed)} / {fmtSeconds(REC_MAX_SECONDS)}
          </span>
          <Button icon="pause" onClick={stopRecording}>Stop</Button>
        </>
      )}
      {phase === "recorded" && clip && (
        <>
          <span className="dropzone-icon"><Icon name="volume" size={20} /></span>
          <span className="t-strong t-num" style={{ fontSize: 13 }}>
            Recording ready · {fmtSeconds(clip.seconds)} · {fmtFileSize(clip.blob.size)}
          </span>
          <span className="row gap-6" style={{ flexWrap: "wrap", justifyContent: "center" }}>
            <Button size="sm" variant="ghost" icon={playing ? "pause" : "play"} onClick={() => void togglePreview()}>
              {playing ? "Stop preview" : "Play"}
            </Button>
            <Button size="sm" variant="ghost" icon="refresh" disabled={disabled} onClick={reRecord}>
              Re-record
            </Button>
            <Button size="sm" variant="primary" icon="check" disabled={disabled} onClick={useRecording}>
              Use Recording
            </Button>
          </span>
        </>
      )}
      {error && <span className="t-micro" style={{ color: "var(--status-critical)" }}>{error}</span>}
    </div>
  );
}

/* ---------- edit modal ---------- */

function EditVoiceModal({ voice, onClose, onSaved }: {
  voice: VoiceProfile | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { toast } = useApp();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [gender, setGender] = useState("neutral");
  const [sampleText, setSampleText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  useEffect(() => {
    if (!voice) return;
    setName(voice.name);
    setDescription(voice.description ?? "");
    setGender(voice.gender ?? "neutral");
    setSampleText(voice.sample ?? "");
    setBusy(false);
    setError("");
    setFieldErrors({});
  }, [voice]);

  const save = async () => {
    if (!voice) return;
    setBusy(true);
    setError("");
    try {
      await updateVoiceClone(voice.id, {
        name: name.trim(),
        description: description.trim(),
        gender,
        sampleText: sampleText.trim(),
      });
      toast("Voice updated", "good");
      onSaved();
    } catch (e) {
      const err = e as ApiRequestError;
      if (err.fieldErrors && Object.keys(err.fieldErrors).length) setFieldErrors(err.fieldErrors);
      setError(err.message || "Update failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={voice !== null}
      onClose={onClose}
      title={voice ? `Edit '${voice.name}'` : "Edit voice"}
      sub="Local details only — the provider voice itself is unchanged"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" busy={busy} disabled={!name.trim()} onClick={() => void save()}>Save</Button>
        </>
      }
    >
      <div className="col gap-12">
        <Field label="Voice name" required error={fieldErrors.name || undefined}>
          <input className="input" value={name} maxLength={100} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Description" error={fieldErrors.description || undefined}>
          <input className="input" value={description} maxLength={500} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <Field label="Gender" error={fieldErrors.gender || undefined}>
          <select className="select" value={gender} onChange={(e) => setGender(e.target.value)} aria-label="Gender">
            <option value="neutral">Neutral</option>
            <option value="female">Female</option>
            <option value="male">Male</option>
          </select>
        </Field>
        <Field label="Sample text" hint="Used as the default preview sentence">
          <input className="input" value={sampleText} maxLength={500} onChange={(e) => setSampleText(e.target.value)} />
        </Field>
        {error && <Callout tone="critical" title="Update failed">{error}</Callout>}
      </div>
    </Modal>
  );
}

/* ---------- preview modal ---------- */

function PreviewCloneModal({ voice, onClose }: { voice: VoiceProfile | null; onClose: () => void }) {
  const [model, setModel] = useState("");
  const [models, setModels] = useState<{ code: string; isDefault: boolean }[]>([]);
  const [language, setLanguage] = useState("");
  const [languages, setLanguages] = useState<string[]>([]);
  const [text, setText] = useState(DEFAULT_SAMPLE_TEXT);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [metrics, setMetrics] = useState<{ ttfaMs: number; totalMs: number } | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState(false);

  useEffect(() => {
    if (!voice?.provider) return;
    let cancelled = false;
    setError("");
    setMetrics(null);
    setText(voice.sample || DEFAULT_SAMPLE_TEXT);
    void (async () => {
      try {
        const all = await listProviderModels("tts", voice.provider!);
        if (cancelled) return;
        setModels(all.map((m) => ({ code: m.code, isDefault: m.isDefault })));
        const preferred =
          all.find((m) => (voice.modelCodes ?? []).includes(m.code) && m.isDefault)?.code ??
          all.find((m) => (voice.modelCodes ?? []).includes(m.code))?.code ??
          all.find((m) => m.isDefault)?.code ?? all[0]?.code ?? "";
        setModel(preferred);
        if (preferred) {
          const langs = await getModelLanguages("tts", voice.provider!, preferred);
          if (cancelled) return;
          const codes = langs.languages.map((l) => l.code);
          setLanguages(codes);
          setLanguage(codes.includes("en-IN") ? "en-IN" : codes[0] ?? "en-IN");
        }
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
      audioRef.current?.pause();
      audioRef.current = null;
    };
  }, [voice]);

  const stop = () => {
    audioRef.current?.pause();
    audioRef.current = null;
    setPlaying(false);
  };

  const generate = async () => {
    if (!voice) return;
    stop();
    setBusy(true);
    setError("");
    setMetrics(null);
    try {
      const r = await generateTtsPreview({
        provider: voice.provider!,
        model,
        voice: voice.id,
        language,
        text: text.trim() || DEFAULT_SAMPLE_TEXT,
      });
      const audio = new Audio(`data:${r.mimeType};base64,${r.audioBase64}`);
      audioRef.current = audio;
      audio.onended = () => setPlaying(false);
      setPlaying(true);
      await audio.play();
      setMetrics({ ttfaMs: r.ttfaMs, totalMs: r.totalMs });
    } catch (e) {
      setPlaying(false);
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={voice !== null}
      onClose={() => { stop(); onClose(); }}
      title={voice ? `Preview '${voice.name}'` : "Preview voice"}
      sub="Generates real audio with your cloned voice (billed like normal TTS)"
      footer={
        <>
          <Button variant="secondary" onClick={() => { stop(); onClose(); }}>Close</Button>
          {playing && <Button variant="ghost" icon="pause" onClick={stop}>Stop</Button>}
          <Button variant="primary" icon="play" busy={busy} disabled={!model || !text.trim()} onClick={() => void generate()}>
            Generate
          </Button>
        </>
      }
    >
      <div className="col gap-12">
        <div className="grid grid-2">
          <Field label="Model">
            <select className="select" value={model} onChange={(e) => setModel(e.target.value)} aria-label="Preview model">
              {models.map((m) => <option key={m.code} value={m.code}>{m.code}{m.isDefault ? " (default)" : ""}</option>)}
            </select>
          </Field>
          <Field label="Language">
            <select className="select" value={language} onChange={(e) => setLanguage(e.target.value)} aria-label="Preview language">
              {(languages.length ? languages : [language]).map((code) => <option key={code} value={code}>{code}</option>)}
            </select>
          </Field>
        </div>
        <Field label="Sample text" hint={`${text.length}/500`}>
          <textarea
            className="input"
            rows={3}
            value={text}
            maxLength={500}
            onChange={(e) => setText(e.target.value)}
          />
        </Field>
        {metrics && (
          <span className="t-micro t-num">
            Time to first audio {Math.round(metrics.ttfaMs)} ms · total {Math.round(metrics.totalMs)} ms
          </span>
        )}
        {error && <Callout tone="critical" title="Preview failed">{error}</Callout>}
      </div>
    </Modal>
  );
}

/* ---------- delete modal ---------- */

function DeleteVoiceModal({ voice, onClose, onDeleted }: {
  voice: VoiceProfile | null;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const { toast } = useApp();
  const [busy, setBusy] = useState(false);

  const confirm = async () => {
    if (!voice) return;
    setBusy(true);
    try {
      await deleteVoiceClone(voice.id);
      toast(`Voice '${voice.name}' deleted`, "good");
      onDeleted();
    } catch (e) {
      toast((e as Error).message, "error");
      onClose();
    } finally {
      setBusy(false);
    }
  };

  return (
    <ConfirmModal
      open={voice !== null}
      onClose={onClose}
      onConfirm={() => void confirm()}
      title={voice ? `Delete '${voice.name}'?` : "Delete voice"}
      body="This also deletes the voice from the provider account and frees its custom-voice slot. Bots still referencing this voice must be unassigned first. This cannot be undone."
      confirmLabel="Delete voice"
      danger
      busy={busy}
    />
  );
}
