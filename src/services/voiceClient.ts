/* Browser voice client for live bot testing.
   - Creates a voice session (REST) then connects a raw WebSocket to the
     runtime worker (`ws://<host>:<workerPort><wsPath>`).
   - Streams 16 kHz mono Int16 PCM mic audio up in ~32 ms chunks
     (AudioWorklet, ScriptProcessor fallback).
   - Plays 24 kHz mono Int16 PCM bot audio back gaplessly by scheduling
     AudioBuffers at a rolling playhead; interruption clears the queue.
   - Text frames are JSON: {type:"transcript"|"bot_text",text} and
     {type:"event",name:...} — surfaced through callbacks. */

import { createVoiceSession } from "./api";

export type VoiceEventName = "bot_speaking_started" | "bot_speaking_stopped" | "interruption";

export interface VoiceClientCallbacks {
  /** Caller words recognised by the runtime STT. */
  onTranscript?: (text: string) => void;
  /** Bot reply text (what the TTS is speaking). */
  onBotText?: (text: string) => void;
  /** Runtime lifecycle events. */
  onEvent?: (name: VoiceEventName) => void;
  /** Socket closed by the server (not fired after an intentional stop()). */
  onClose?: () => void;
  onError?: (message: string) => void;
}

const MIC_RATE = 16000;
const BOT_RATE = 24000;
const CHUNK_SAMPLES = 512; // 32 ms at 16 kHz — within the 20–40 ms target

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

export class VoiceClient {
  private callbacks: VoiceClientCallbacks;
  private ws: WebSocket | null = null;
  private micStream: MediaStream | null = null;
  private micCtx: AudioContext | null = null;
  private playCtx: AudioContext | null = null;
  private pending = new Float32Array(0);
  private playhead = 0;
  private activeSources = new Set<AudioBufferSourceNode>();
  private stopped = false;

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
    this.playCtx = new AudioContext({ sampleRate: BOT_RATE });
    await this.startCapture(stream);
  }

  /** Stop and clear all queued bot audio (used on interruption). */
  stopPlayback(): void {
    for (const src of this.activeSources) {
      src.onended = null;
      try { src.stop(); } catch { /* already stopped */ }
    }
    this.activeSources.clear();
    this.playhead = 0;
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
      ws.onclose = () => {
        if (!this.stopped) this.callbacks.onClose?.();
      };
      ws.onmessage = (ev: MessageEvent) => this.handleMessage(ev.data as unknown);
      this.ws = ws;
    });
  }

  private handleMessage(data: unknown) {
    if (typeof data === "string") {
      let msg: { type?: string; text?: string; name?: string };
      try {
        msg = JSON.parse(data) as { type?: string; text?: string; name?: string };
      } catch {
        return;
      }
      if (msg.type === "transcript" && msg.text) {
        this.callbacks.onTranscript?.(msg.text);
      } else if (msg.type === "bot_text" && msg.text) {
        this.callbacks.onBotText?.(msg.text);
      } else if (msg.type === "event" && msg.name) {
        const name = msg.name as VoiceEventName;
        if (name === "interruption") this.stopPlayback();
        this.callbacks.onEvent?.(name);
      }
      return;
    }
    if (data instanceof ArrayBuffer) this.enqueueAudio(data);
  }

  /* ---------- playback (24 kHz Int16 PCM → gapless AudioBuffers) ---------- */

  private enqueueAudio(buf: ArrayBuffer) {
    const ctx = this.playCtx;
    if (!ctx || buf.byteLength < 2) return;
    const int16 = new Int16Array(buf, 0, Math.floor(buf.byteLength / 2));
    const f32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) f32[i] = int16[i] / 32768;
    const audio = ctx.createBuffer(1, f32.length, BOT_RATE);
    audio.copyToChannel(f32, 0);
    const src = ctx.createBufferSource();
    src.buffer = audio;
    src.connect(ctx.destination);
    const startAt = Math.max(this.playhead, ctx.currentTime + 0.03);
    src.start(startAt);
    this.playhead = startAt + audio.duration;
    this.activeSources.add(src);
    src.onended = () => this.activeSources.delete(src);
  }

  /* ---------- capture (mic Float32 → 16 kHz Int16 PCM chunks) ---------- */

  private async startCapture(stream: MediaStream) {
    const ctx = new AudioContext({ sampleRate: MIC_RATE });
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
    const merged = new Float32Array(this.pending.length + frame.length);
    merged.set(this.pending, 0);
    merged.set(frame, this.pending.length);
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
