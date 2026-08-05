/* Browser voice client for live bot testing.
   - Creates a voice session (REST) then connects a raw WebSocket to the
     runtime worker (`ws://<host>:<workerPort><wsPath>`).
   - Streams 16 kHz mono Int16 PCM mic audio up in ~32 ms chunks
     (AudioWorklet, ScriptProcessor fallback); if the capture AudioContext
     refuses to run at 16 kHz the samples are linearly resampled first.
   - Plays bot audio gaplessly at THE RATE THE WORKER DECLARES in its
     `session_config` message (never a hardcoded rate — the worker streams
     whatever the bot's audio settings say, e.g. 16 kHz, and playing that at
     an assumed 24 kHz is fast, pitch-shifted and choppy).
   - Interruption clears the queue and suppresses any stale audio frames
     until the next bot reply actually starts.
   - Text frames are JSON: {type:"session_config"|"transcript"|"bot_text"|
     "language"|"error"} and {type:"event",name:...}. */

import { createVoiceSession } from "./api";

export type VoiceEventName =
  | "bot_speaking_started"
  | "bot_speaking_stopped"
  | "interruption"
  | "language_unsupported";

export interface VoiceSessionConfig {
  botName?: string;
  sampleRate?: number;
  language?: string;
  languages?: string[];
  voices?: Record<string, { provider: string; voice: string; gender?: string }>;
  defaultVoice?: { provider: string; voice: string; gender?: string };
  warnings?: Record<string, string>;
}

export interface VoiceClientCallbacks {
  /** Session parameters announced by the runtime before the greeting. */
  onSessionConfig?: (config: VoiceSessionConfig) => void;
  /** Caller words recognised by the runtime STT. */
  onTranscript?: (text: string, at?: string) => void;
  /** Bot reply text (what the TTS is speaking). */
  onBotText?: (text: string, at?: string) => void;
  /** The conversation switched to following this language. */
  onLanguage?: (locale: string) => void;
  /** Runtime lifecycle events. */
  onEvent?: (name: VoiceEventName, detail?: Record<string, unknown>) => void;
  /** Socket closed by the server (not fired after an intentional stop()). */
  onClose?: () => void;
  onError?: (message: string) => void;
}

const MIC_RATE = 16000;
const DEFAULT_BOT_RATE = 24000; // only until session_config announces the real rate
const CHUNK_SAMPLES = 512; // 32 ms at 16 kHz — within the 20–40 ms target

/* Friendly messages for the worker's application close codes. */
const CLOSE_MESSAGES: Record<number, string> = {
  4401: "The voice session expired or is unknown — start a new session.",
  4403: "This session is not allowed for the selected bot.",
  4404: "The bot's voice configuration could not be loaded.",
  4429: "The voice worker is at capacity — try again in a moment.",
  4500: "Voice engine configuration error — check the bot's provider and voice settings.",
};

/** Linear resampler for mono Float32 PCM (capture-path safety net). */
export function downsampleLinear(
  input: Float32Array,
  fromRate: number,
  toRate: number,
): Float32Array {
  if (fromRate === toRate || input.length === 0) return input;
  const outLength = Math.max(1, Math.round((input.length * toRate) / fromRate));
  const out = new Float32Array(outLength);
  const step = (input.length - 1) / Math.max(1, outLength - 1);
  for (let i = 0; i < outLength; i++) {
    const pos = i * step;
    const i0 = Math.floor(pos);
    const i1 = Math.min(input.length - 1, i0 + 1);
    out[i] = input[i0] + (input[i1] - input[i0]) * (pos - i0);
  }
  return out;
}

/* Minimal slice of AudioContext the playback queue needs — injectable for tests. */
export interface PlaybackContextLike {
  currentTime: number;
  destination: AudioNode;
  createBuffer(channels: number, length: number, sampleRate: number): AudioBuffer;
  createBufferSource(): AudioBufferSourceNode;
}

/** Gapless sequential scheduler for Int16 PCM chunks at a fixed sample rate.

    Every chunk is scheduled at the rolling playhead (never "immediately"),
    so chunks can neither overlap nor race each other; a chunk arriving after
    the playhead has passed re-anchors with a small lead instead of playing
    in the past. `stop()` cancels everything queued (barge-in). */
export class PcmPlaybackQueue {
  private playhead = 0;
  private active = new Set<AudioBufferSourceNode>();

  constructor(
    private ctx: PlaybackContextLike,
    readonly sampleRate: number,
    private leadSeconds = 0.04,
  ) {}

  enqueue(buf: ArrayBuffer): void {
    const samples = Math.floor(buf.byteLength / 2);
    if (samples <= 0) return;
    const int16 = new Int16Array(buf, 0, samples);
    const f32 = new Float32Array(samples);
    for (let i = 0; i < samples; i++) f32[i] = int16[i] / 32768;
    const audio = this.ctx.createBuffer(1, samples, this.sampleRate);
    audio.copyToChannel(f32, 0);
    const src = this.ctx.createBufferSource();
    src.buffer = audio;
    src.connect(this.ctx.destination);
    const startAt = Math.max(this.playhead, this.ctx.currentTime + this.leadSeconds);
    src.start(startAt);
    this.playhead = startAt + samples / this.sampleRate;
    this.active.add(src);
    src.onended = () => this.active.delete(src);
  }

  /** Stop and discard everything scheduled (used on interruption). */
  stop(): void {
    for (const src of this.active) {
      src.onended = null;
      try {
        src.stop();
      } catch {
        /* already stopped */
      }
    }
    this.active.clear();
    this.playhead = 0;
  }

  get activeCount(): number {
    return this.active.size;
  }
}

export class VoiceClient {
  private callbacks: VoiceClientCallbacks;
  private ws: WebSocket | null = null;
  private micStream: MediaStream | null = null;
  private micCtx: AudioContext | null = null;
  private playCtx: AudioContext | null = null;
  private playback: PcmPlaybackQueue | null = null;
  private botRate = DEFAULT_BOT_RATE;
  private pending = new Float32Array(0);
  private stopped = false;
  /* Barge-in gate: audio frames that were already in flight when the server
     interrupted are dropped until the next reply actually starts speaking. */
  private suppressAudio = false;
  sessionConfig: VoiceSessionConfig | null = null;

  constructor(callbacks: VoiceClientCallbacks = {}) {
    this.callbacks = callbacks;
  }

  /** Create the session, acquire the mic, connect the socket, start streaming. */
  async start(botId: string): Promise<void> {
    const session = await createVoiceSession(botId, "browser");

    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { sampleRate: MIC_RATE, channelCount: 1, echoCancellation: true },
      });
    } catch {
      throw new Error("Microphone access was denied — allow the microphone for this site and try again.");
    }
    this.micStream = stream;

    await this.openSocket(`ws://${location.hostname}:${session.workerPort}${session.wsPath}`);
    await this.startCapture(stream);
  }

  /** Stop and clear all queued bot audio (used on interruption). */
  stopPlayback(): void {
    this.playback?.stop();
  }

  /** Tear the session down: socket, mic tracks, audio contexts. */
  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.stopPlayback();
    if (this.ws) {
      try { this.ws.close(); } catch { /* already closed */ }
      this.ws = null;
    }
    this.micStream?.getTracks().forEach((t) => t.stop());
    this.micStream = null;
    void this.micCtx?.close().catch(() => undefined);
    this.micCtx = null;
    void this.playCtx?.close().catch(() => undefined);
    this.playCtx = null;
    this.playback = null;
    this.pending = new Float32Array(0);
  }

  /* ---------- socket ---------- */

  private openSocket(url: string): Promise<void> {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
      ws.onopen = () => resolve();
      ws.onerror = () => {
        if (!this.stopped) this.callbacks.onError?.("Voice connection error — the runtime worker may be unreachable.");
        reject(new Error("Could not connect to the voice runtime worker."));
      };
      ws.onclose = (ev: CloseEvent) => {
        if (this.stopped) return;
        const message = CLOSE_MESSAGES[ev.code];
        if (message) this.callbacks.onError?.(message);
        this.callbacks.onClose?.();
      };
      ws.onmessage = (ev: MessageEvent) => this.handleMessage(ev.data as unknown);
      this.ws = ws;
    });
  }

  /** Exposed for tests — routes a raw WS payload through the client. */
  handleMessage(data: unknown): void {
    if (typeof data === "string") {
      let msg: { type?: string; text?: string; name?: string; message?: string;
                 language?: string; sampleRate?: number; at?: string } & VoiceSessionConfig;
      try {
        msg = JSON.parse(data) as typeof msg;
      } catch {
        return;
      }
      if (msg.type === "session_config") {
        this.applySessionConfig(msg);
      } else if (msg.type === "transcript" && msg.text) {
        this.callbacks.onTranscript?.(msg.text, msg.at);
      } else if (msg.type === "bot_text" && msg.text) {
        this.suppressAudio = false; // a new reply is definitely underway
        this.callbacks.onBotText?.(msg.text, msg.at);
      } else if (msg.type === "language" && msg.language) {
        this.callbacks.onLanguage?.(msg.language);
      } else if (msg.type === "error" && msg.message) {
        this.callbacks.onError?.(`Voice runtime error: ${msg.message}`);
      } else if (msg.type === "event" && msg.name) {
        const name = msg.name as VoiceEventName;
        if (name === "interruption") {
          this.stopPlayback();
          this.suppressAudio = true; // drop in-flight stale chunks
        } else if (name === "bot_speaking_started") {
          this.suppressAudio = false; // the next reply's audio is authoritative
        }
        this.callbacks.onEvent?.(name, { language: msg.language });
      }
      return;
    }
    if (data instanceof ArrayBuffer) {
      if (this.suppressAudio) return;
      this.ensurePlayback().enqueue(data);
    }
  }

  /* ---------- playback (server-declared rate, gapless scheduling) ---------- */

  private applySessionConfig(msg: VoiceSessionConfig): void {
    this.sessionConfig = msg;
    const rate = Number(msg.sampleRate);
    if (Number.isFinite(rate) && rate >= 8000 && rate <= 48000 && rate !== this.botRate) {
      this.botRate = rate;
      // Rebuild the playback pipeline at the announced rate. session_config
      // always precedes the greeting audio, so nothing is queued yet.
      this.playback?.stop();
      void this.playCtx?.close().catch(() => undefined);
      this.playCtx = null;
      this.playback = null;
    }
    this.callbacks.onSessionConfig?.(msg);
  }

  private ensurePlayback(): PcmPlaybackQueue {
    if (!this.playback) {
      this.playCtx = new AudioContext({ sampleRate: this.botRate });
      this.playback = new PcmPlaybackQueue(this.playCtx, this.botRate);
    }
    return this.playback;
  }

  /* ---------- capture (mic Float32 → 16 kHz Int16 PCM chunks) ---------- */

  private async startCapture(stream: MediaStream) {
    let ctx: AudioContext;
    try {
      ctx = new AudioContext({ sampleRate: MIC_RATE });
    } catch {
      ctx = new AudioContext(); // browser refused the rate — resample instead
    }
    this.micCtx = ctx;
    await ctx.resume();
    const source = ctx.createMediaStreamSource(stream);
    /* Keep the graph pulled without producing sound. */
    const silence = ctx.createGain();
    silence.gain.value = 0;
    silence.connect(ctx.destination);

    if (ctx.audioWorklet) {
      const moduleUrl = URL.createObjectURL(new Blob([CAPTURE_WORKLET], { type: "application/javascript" }));
      try {
        await ctx.audioWorklet.addModule(moduleUrl);
      } finally {
        URL.revokeObjectURL(moduleUrl);
      }
      const node = new AudioWorkletNode(ctx, "pcm-capture");
      node.port.onmessage = (e: MessageEvent<Float32Array>) => this.pushSamples(e.data);
      source.connect(node);
      node.connect(silence);
    } else {
      const node = ctx.createScriptProcessor(1024, 1, 1);
      node.onaudioprocess = (e: AudioProcessingEvent) =>
        this.pushSamples(e.inputBuffer.getChannelData(0).slice(0));
      source.connect(node);
      node.connect(silence);
    }
  }

  private pushSamples(frame: Float32Array) {
    const ws = this.ws;
    if (this.stopped || !ws || ws.readyState !== WebSocket.OPEN) return;
    const captureRate = this.micCtx?.sampleRate ?? MIC_RATE;
    const samples = captureRate === MIC_RATE ? frame : downsampleLinear(frame, captureRate, MIC_RATE);
    const merged = new Float32Array(this.pending.length + samples.length);
    merged.set(this.pending, 0);
    merged.set(samples, this.pending.length);
    let offset = 0;
    while (merged.length - offset >= CHUNK_SAMPLES) {
      const int16 = new Int16Array(CHUNK_SAMPLES);
      for (let i = 0; i < CHUNK_SAMPLES; i++) {
        const v = Math.max(-1, Math.min(1, merged[offset + i]));
        int16[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
      }
      ws.send(int16.buffer);
      offset += CHUNK_SAMPLES;
    }
    this.pending = merged.slice(offset);
  }
}

const CAPTURE_WORKLET = `
class PcmCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length) this.port.postMessage(channel.slice(0));
    return true;
  }
}
registerProcessor("pcm-capture", PcmCaptureProcessor);
`;
