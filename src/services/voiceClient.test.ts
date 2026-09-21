/* Voice client: playback scheduling, sample-rate handling, resampling and
   the stale-audio gate. AudioContext is faked — the queue's scheduling math
   (sequential playhead, no overlap, cancellation) is what's under test. */
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  PcmPlaybackQueue,
  VoiceClient,
  downsampleLinear,
  voiceSocketUrl,
  type PlaybackContextLike,
} from "./voiceClient";

/* ---------- fakes ---------- */

class FakeSource {
  buffer: { duration: number; length: number } | null = null;
  started: number[] = [];
  stopped = false;
  stoppedAt: number | null = null;
  onended: (() => void) | null = null;
  connect = vi.fn();
  constructor(private now: () => number = () => 0) {}
  start(at: number) {
    this.started.push(at);
  }
  stop(at = this.now()) {
    this.stopped = true;
    this.stoppedAt = at;
  }
}

function fakeContext(_sampleRate: number) {
  const sources: FakeSource[] = [];
  const ctx = {
    currentTime: 0,
    destination: {} as AudioNode,
    createGain() {
      return {
        connect: vi.fn(), disconnect: vi.fn(),
        gain: { setValueAtTime: vi.fn(), linearRampToValueAtTime: vi.fn() },
      } as unknown as GainNode;
    },
    createBuffer(_ch: number, length: number, rate: number) {
      return {
        duration: length / rate,
        length,
        copyToChannel: vi.fn(),
      } as unknown as AudioBuffer;
    },
    createBufferSource() {
      const src = new FakeSource(() => ctx.currentTime);
      sources.push(src);
      return src as unknown as AudioBufferSourceNode;
    },
  } as unknown as PlaybackContextLike & { currentTime: number };
  return { ctx, sources };
}

function pcmChunk(samples: number): ArrayBuffer {
  return new Int16Array(samples).buffer;
}

/* ---------- resampler ---------- */

describe("downsampleLinear", () => {
  it("is identity at equal rates", () => {
    const input = new Float32Array([0.1, 0.2, 0.3]);
    expect(downsampleLinear(input, 16000, 16000)).toBe(input);
  });

  it("halves the sample count from 32 kHz to 16 kHz", () => {
    const input = new Float32Array(3200);
    expect(downsampleLinear(input, 32000, 16000).length).toBe(1600);
  });

  it("interpolates between neighbours", () => {
    const out = downsampleLinear(new Float32Array([0, 1]), 32000, 16000);
    expect(out.length).toBe(1);
    expect(out[0]).toBeCloseTo(0);
  });
});

/* ---------- playback queue ---------- */

describe("PcmPlaybackQueue", () => {
  let ctx: ReturnType<typeof fakeContext>["ctx"];
  let sources: FakeSource[];
  let queue: PcmPlaybackQueue;

  beforeEach(() => {
    const fake = fakeContext(16000);
    ctx = fake.ctx;
    sources = fake.sources;
    queue = new PcmPlaybackQueue(ctx, 16000, 0.04);
  });

  it("schedules chunks strictly sequentially (order preserved, no overlap)", () => {
    queue.enqueue(pcmChunk(1600)); // 100 ms
    queue.enqueue(pcmChunk(800)); // 50 ms
    queue.enqueue(pcmChunk(1600));
    const starts = sources.map((s) => s.started[0]);
    expect(starts[0]).toBeCloseTo(0.04);
    expect(starts[1]).toBeCloseTo(0.14); // exactly after chunk 1
    expect(starts[2]).toBeCloseTo(0.19); // exactly after chunk 2
  });

  it("uses the queue's sample rate for chunk durations", () => {
    const fake = fakeContext(24000);
    const q24 = new PcmPlaybackQueue(fake.ctx, 24000, 0.04);
    q24.enqueue(pcmChunk(2400)); // 100 ms at 24 kHz
    q24.enqueue(pcmChunk(2400));
    expect(fake.sources[1].started[0]).toBeCloseTo(0.14);
  });

  it("re-anchors after a network gap instead of scheduling in the past", () => {
    queue.enqueue(pcmChunk(160)); // 10 ms → playhead 0.05
    (ctx as { currentTime: number }).currentTime = 1.0; // long gap
    queue.enqueue(pcmChunk(160));
    expect(sources[1].started[0]).toBeCloseTo(1.04);
  });

  it("stop() cancels every scheduled source and resets the playhead", () => {
    queue.enqueue(pcmChunk(1600));
    queue.enqueue(pcmChunk(1600));
    queue.stop();
    expect(sources.every((s) => s.stopped)).toBe(true);
    expect(queue.activeCount).toBe(0);
    queue.enqueue(pcmChunk(1600));
    expect(sources[2].started[0]).toBeCloseTo(0.04); // fresh anchor, not old playhead
  });

  it("ignores empty and odd-length buffers", () => {
    queue.enqueue(new ArrayBuffer(0));
    queue.enqueue(new ArrayBuffer(1));
    expect(sources.length).toBe(0);
  });

  it.each([0.041, 0.095, 0.155])(
    "clears queued and playing filler at readiness %s without delaying response chunks",
    (readyAt) => {
      for (let i = 0; i < 6; i++) queue.enqueue(pcmChunk(320), "call-A-turn-1");
      ctx.currentTime = readyAt;
      queue.clearFiller("call-A-turn-1");
      queue.enqueue(pcmChunk(320));
      queue.enqueue(pcmChunk(320));

      expect(sources.slice(0, 6).every((s) => s.stoppedAt! <= readyAt + 0.002)).toBe(true);
      expect(queue.activeCount).toBe(2);
      expect(sources[6].started[0]).toBeCloseTo(readyAt + 0.002, 6);
      expect(sources[7].started[0]).toBeCloseTo(readyAt + 0.022, 6);
      const sounding = sources.slice(0, 6).find((s) => s.started[0] <= readyAt && s.started[0] + 0.02 > readyAt)!;
      const gain = sounding.connect.mock.calls[0][0] as GainNode;
      expect(gain.gain.setValueAtTime).toHaveBeenCalledWith(1, readyAt);
      expect(gain.gain.linearRampToValueAtTime).toHaveBeenCalledWith(0, readyAt + 0.002);
      // The stopped filler cannot overlap or precede the response at the API
      // scheduler. This fake does not model already-rendered device samples.
      expect(sources.slice(0, 6).every((s) => s.stoppedAt! <= sources[6].started[0])).toBe(true);
    },
  );

  it("preserves binary audio and another turn's filler when clearing one owner", () => {
    queue.enqueue(pcmChunk(1600));
    queue.enqueue(pcmChunk(1600), "turn-A");
    queue.enqueue(pcmChunk(1600), "turn-B");
    ctx.currentTime = 0.06;
    queue.clearFiller("turn-A");
    queue.enqueue(pcmChunk(320));

    expect(sources.map((s) => s.stopped)).toEqual([false, true, false, false]);
    expect(sources[0].started[0]).toBeCloseTo(0.04);
    expect(sources[2].started[0]).toBeCloseTo(0.24);
    expect(sources[3].started[0]).toBeCloseTo(0.34);
  });

  it("does not let an unknown owner's clear alter valid playback timing", () => {
    queue.enqueue(pcmChunk(1600));
    queue.clearFiller("another-call");
    ctx.currentTime = 0.2;
    queue.enqueue(pcmChunk(320));
    expect(sources[0].stopped).toBe(false);
    expect(sources[1].started[0]).toBeCloseTo(0.24);
  });

  it("rejects delayed packets from a cleared owner, while later turns still play", () => {
    queue.enqueue(pcmChunk(320), "turn-A");
    queue.clearFiller("turn-A");
    queue.enqueue(pcmChunk(320), "turn-A");
    queue.enqueue(pcmChunk(320), "turn-B");
    expect(sources).toHaveLength(2);
    expect(sources[1].stopped).toBe(false);
  });

  it("keeps filler cancellation state isolated between calls", () => {
    const other = fakeContext(16000);
    const otherQueue = new PcmPlaybackQueue(other.ctx, 16000);
    queue.enqueue(pcmChunk(320), "turn-1");
    otherQueue.enqueue(pcmChunk(320), "turn-1");
    queue.clearFiller("turn-1");
    otherQueue.enqueue(pcmChunk(320), "turn-1");
    expect(sources[0].stopped).toBe(true);
    expect(other.sources.every((s) => !s.stopped)).toBe(true);
    expect(other.sources).toHaveLength(2);
  });

  it("clears six consecutive turns without leaking sources or a stale playhead", () => {
    for (let turn = 0; turn < 6; turn++) {
      ctx.currentTime = turn;
      const start = sources.length;
      for (let i = 0; i < 3; i++) queue.enqueue(pcmChunk(320), `turn-${turn}`);
      ctx.currentTime = turn + 0.05;
      queue.clearFiller(`turn-${turn}`);
      queue.enqueue(pcmChunk(320));
      expect(sources[start + 3].started[0]).toBeCloseTo(ctx.currentTime + 0.002, 6);
      expect(sources.slice(start, start + 3).every((s) => s.stopped)).toBe(true);
      queue.enqueue(pcmChunk(320), `turn-${turn}`);
      expect(sources).toHaveLength(start + 4);
      sources[start + 3].onended?.();
      expect(queue.activeCount).toBe(0);
    }
    ctx.currentTime = 7;
    queue.enqueue(pcmChunk(320)); // ordinary later fast turn retains its lead
    expect(sources.at(-1)!.started[0]).toBeCloseTo(7.04);
  });
});

/* ---------- client message handling ---------- */

function clientWithQueue() {
  const fake = fakeContext(16000);
  const events: Record<string, unknown[]> = { config: [], language: [], errors: [] };
  const client = new VoiceClient({
    onSessionConfig: (c) => events.config.push(c),
    onLanguage: (l) => events.language.push(l),
    onError: (m) => events.errors.push(m),
  });
  // Inject a fake playback pipeline (jsdom has no AudioContext).
  const queue = new PcmPlaybackQueue(fake.ctx, 16000, 0.04);
  const internals = client as unknown as {
    ensurePlayback: () => PcmPlaybackQueue;
    playback: PcmPlaybackQueue | null;
  };
  internals.ensurePlayback = () => queue;
  internals.playback = queue; // stopPlayback() must reach the same queue
  return { client, queue, sources: fake.sources, ctx: fake.ctx, events };
}

function fillerMessage(owner: string, samples = 320): string {
  const bytes = new Uint8Array(pcmChunk(samples));
  return JSON.stringify({ type: "filler_audio", owner, audio: btoa(String.fromCharCode(...bytes)) });
}

describe("VoiceClient message handling", () => {
  it("passes the runtime turn timestamp through for transcript and bot text", () => {
    const onTranscript = vi.fn();
    const onBotText = vi.fn();
    const client = new VoiceClient({ onTranscript, onBotText });

    client.handleMessage(JSON.stringify({
      type: "transcript", text: "हाँ", at: "2026-08-05T07:57:38.001234Z",
    }));
    client.handleMessage(JSON.stringify({
      type: "bot_text", text: "जी", at: "2026-08-05T07:57:39.728901Z",
    }));

    expect(onTranscript).toHaveBeenCalledWith("हाँ", "2026-08-05T07:57:38.001234Z");
    expect(onBotText).toHaveBeenCalledWith("जी", "2026-08-05T07:57:39.728901Z");
  });

  it("turn_rewound surfaces the retracted user (and optional bot) text", () => {
    const onTurnRewound = vi.fn();
    const client = new VoiceClient({ onTurnRewound });

    client.handleMessage(JSON.stringify({ type: "turn_rewound", user_text: "हाँ।" }));
    client.handleMessage(JSON.stringify({
      type: "turn_rewound", user_text: "मेरा मतलब,", bot_text: "कृपया पूरा बताइए।",
    }));

    expect(onTurnRewound).toHaveBeenNthCalledWith(1, "हाँ।", undefined);
    expect(onTurnRewound).toHaveBeenNthCalledWith(2, "मेरा मतलब,", "कृपया पूरा बताइए।");
  });

  it("session_config is stored and surfaced with the announced sample rate", () => {
    const { client, events } = clientWithQueue();
    client.handleMessage(
      JSON.stringify({ type: "session_config", sampleRate: 16000, language: "hi-IN" }),
    );
    expect(client.sessionConfig?.sampleRate).toBe(16000);
    expect(events.config).toHaveLength(1);
  });

  it("plays audio chunks and drops stale ones after an interruption", () => {
    const { client, sources } = clientWithQueue();
    client.handleMessage(pcmChunk(1600));
    expect(sources).toHaveLength(1);
    client.handleMessage(JSON.stringify({ type: "event", name: "interruption" }));
    expect(sources[0].stopped).toBe(true);
    client.handleMessage(pcmChunk(1600)); // stale in-flight chunk → dropped
    expect(sources).toHaveLength(1);
    client.handleMessage(JSON.stringify({ type: "event", name: "bot_speaking_started" }));
    client.handleMessage(pcmChunk(1600)); // the new reply plays again
    expect(sources).toHaveLength(2);
  });

  it("bot_text also lifts the stale-audio gate", () => {
    const { client, sources } = clientWithQueue();
    client.handleMessage(JSON.stringify({ type: "event", name: "interruption" }));
    client.handleMessage(JSON.stringify({ type: "bot_text", text: "Namaskar" }));
    client.handleMessage(pcmChunk(1600));
    expect(sources).toHaveLength(1);
  });

  it("routes tagged filler and targeted clear before binary response PCM", () => {
    const { client, sources, ctx } = clientWithQueue();
    client.handleMessage(fillerMessage("call-A-turn-1"));
    client.handleMessage(fillerMessage("call-A-turn-1"));
    ctx.currentTime = 0.045;
    client.handleMessage(JSON.stringify({ type: "filler_clear", owner: "call-A-turn-1" }));
    client.handleMessage(pcmChunk(320));
    expect(sources.map((s) => s.stopped)).toEqual([true, true, false]);
    expect(sources[2].started[0]).toBeCloseTo(0.047, 6);
    client.handleMessage(fillerMessage("call-A-turn-1"));
    expect(sources).toHaveLength(3);
  });

  it("preserves real binary PCM even when its samples match filler PCM", () => {
    const { client, sources } = clientWithQueue();
    client.handleMessage(pcmChunk(320));
    client.handleMessage(fillerMessage("turn-1"));
    client.handleMessage(JSON.stringify({ type: "filler_clear", owner: "turn-1" }));
    expect(sources.map((s) => s.stopped)).toEqual([false, true]);
  });

  it("rejects filler arriving after its clear, including before any audio was scheduled", () => {
    const { client, sources } = clientWithQueue();
    client.handleMessage(JSON.stringify({ type: "filler_clear", owner: "old-turn" }));
    client.handleMessage(fillerMessage("old-turn"));
    expect(sources).toHaveLength(0);
    client.handleMessage(fillerMessage("next-turn"));
    expect(sources).toHaveLength(1);
  });

  it("allows new-turn filler while keeping interrupted binary audio gated", () => {
    const { client, sources } = clientWithQueue();
    client.handleMessage(JSON.stringify({ type: "event", name: "interruption" }));
    client.handleMessage(JSON.stringify({ type: "filler_clear", owner: "old-turn" }));
    client.handleMessage(fillerMessage("next-turn"));
    client.handleMessage(pcmChunk(320));
    expect(sources).toHaveLength(1);
    client.handleMessage(JSON.stringify({ type: "bot_text", text: "A reply" }));
    client.handleMessage(pcmChunk(320));
    expect(sources).toHaveLength(2);
  });

  it("retires completed and playing owners across six interruptions", () => {
    const { client, sources, ctx } = clientWithQueue();
    for (let turn = 0; turn < 6; turn++) {
      ctx.currentTime = turn;
      client.handleMessage(fillerMessage(`turn-${turn}`));
      expect(sources).toHaveLength(turn + 1);
      if (turn % 2 === 0) sources.at(-1)!.onended?.();
      client.handleMessage(JSON.stringify({ type: "event", name: "interruption" }));
      for (let old = 0; old <= turn; old++) client.handleMessage(fillerMessage(`turn-${old}`));
      client.handleMessage(pcmChunk(320));
      expect(sources).toHaveLength(turn + 1);
    }
    client.handleMessage(fillerMessage("new-slow-turn"));
    expect(sources).toHaveLength(7);
  });

  it("stops both the retiring filler taper and response at a handoff interruption", () => {
    const { client, sources, ctx } = clientWithQueue();
    client.handleMessage(fillerMessage("turn-1"));
    ctx.currentTime = 0.045;
    client.handleMessage(JSON.stringify({ type: "filler_clear", owner: "turn-1" }));
    client.handleMessage(pcmChunk(320));
    ctx.currentTime = 0.046;
    client.handleMessage(JSON.stringify({ type: "event", name: "interruption" }));
    expect(sources.every((s) => s.stoppedAt === ctx.currentTime)).toBe(true);
    client.handleMessage(fillerMessage("turn-1"));
    client.handleMessage(fillerMessage("turn-2"));
    expect(sources).toHaveLength(3);
  });

  it("isolates interruption and teardown from another call", () => {
    const a = clientWithQueue();
    const b = clientWithQueue();
    a.client.handleMessage(fillerMessage("call-A-turn-1"));
    b.client.handleMessage(fillerMessage("call-B-turn-1"));
    a.client.handleMessage(JSON.stringify({ type: "event", name: "interruption" }));
    expect(b.sources[0].stopped).toBe(false);
    a.client.stop();
    a.client.handleMessage(fillerMessage("late-call-A"));
    expect(a.sources).toHaveLength(1);
    b.client.handleMessage(fillerMessage("call-B-turn-1"));
    expect(b.sources).toHaveLength(2);
  });

  it("ignores queued messages from a superseded call socket", async () => {
    const sockets: FakeSocket[] = [];
    class FakeSocket {
      binaryType = "";
      onopen: (() => void) | null = null;
      onmessage: ((event: { data: unknown }) => void) | null = null;
      close = vi.fn();
      constructor() { sockets.push(this); }
    }
    vi.stubGlobal("WebSocket", FakeSocket);
    try {
      const { client, sources } = clientWithQueue();
      const open = (client as unknown as { openSocket: (url: string) => Promise<void> }).openSocket.bind(client);
      const first = open("ws://old-call");
      sockets[0].onopen?.();
      await first;
      const second = open("ws://new-call");
      sockets[1].onopen?.();
      await second;
      sockets[1].onmessage?.({ data: fillerMessage("new-call-turn-1") });
      sockets[0].onmessage?.({ data: JSON.stringify({ type: "event", name: "interruption" }) });
      sockets[0].onmessage?.({ data: fillerMessage("old-call-turn-1") });
      expect(sources).toHaveLength(1);
      expect(sources[0].stopped).toBe(false);
      client.stop();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("ignores malformed tagged filler packets without affecting real PCM", () => {
    const { client, sources } = clientWithQueue();
    for (const msg of [
      { type: "filler_audio", owner: "turn", audio: "!invalid base64!" },
      { type: "filler_audio", owner: {}, audio: "AAAA" },
      { type: "filler_audio", owner: "", audio: "AAAA" },
      { type: "filler_clear", owner: {} },
    ]) client.handleMessage(JSON.stringify(msg));
    expect(sources).toHaveLength(0);
    client.handleMessage(pcmChunk(320));
    expect(sources).toHaveLength(1);
  });

  it("language messages update the conversation language", () => {
    const { client, events } = clientWithQueue();
    client.handleMessage(JSON.stringify({ type: "language", language: "en-IN" }));
    expect(events.language).toEqual(["en-IN"]);
  });

  it("runtime errors are surfaced, not swallowed", () => {
    const { client, events } = clientWithQueue();
    client.handleMessage(JSON.stringify({ type: "error", message: "tts_failure:timeout" }));
    expect(events.errors[0]).toContain("tts_failure:timeout");
  });

  it("malformed JSON is ignored without crashing", () => {
    const { client } = clientWithQueue();
    expect(() => client.handleMessage("{not json")).not.toThrow();
  });
});

describe("VoiceClient mic mute", () => {
  it("keeps streaming zeroed chunks while muted, real audio after unmute", () => {
    const client = new VoiceClient();
    const sent: ArrayBuffer[] = [];
    const internals = client as unknown as {
      ws: { readyState: number; send: (b: ArrayBuffer) => void } | null;
      pushSamples: (frame: Float32Array) => void;
    };
    internals.ws = { readyState: WebSocket.OPEN, send: (b) => sent.push(b) };
    const frame = new Float32Array(512).fill(0.5); // exactly one 512-sample chunk

    internals.pushSamples(frame);
    expect(sent).toHaveLength(1);
    expect(new Int16Array(sent[0]).some((v) => v !== 0)).toBe(true);

    client.setMuted(true);
    expect(client.isMuted).toBe(true);
    internals.pushSamples(frame);
    expect(sent).toHaveLength(2); // the stream never stops — silence, not a stall
    expect(new Int16Array(sent[1]).every((v) => v === 0)).toBe(true);

    client.setMuted(false);
    internals.pushSamples(frame);
    expect(new Int16Array(sent[2]).some((v) => v !== 0)).toBe(true);
  });
});

describe("voiceSocketUrl", () => {
  const session = { wsPath: "/ws/voice/vs-1", workerPort: 9002 };
  const http = { protocol: "http:", hostname: "localhost", host: "localhost:5199" };
  const https = { protocol: "https:", hostname: "app.example.com", host: "app.example.com" };

  it("addresses the worker port directly over plain HTTP (local dev)", () => {
    expect(voiceSocketUrl(session, http)).toBe("ws://localhost:9002/ws/voice/vs-1");
  });

  it("never emits ws:// from an HTTPS page — browsers block that outright", () => {
    const url = voiceSocketUrl(session, https);
    expect(url.startsWith("wss://")).toBe(true);
    expect(url).toBe("wss://app.example.com/ws/voice/vs-1");
  });

  it("keeps a non-default HTTPS port so the proxy origin still matches", () => {
    expect(voiceSocketUrl(session, { ...https, host: "app.example.com:8443" }))
      .toBe("wss://app.example.com:8443/ws/voice/vs-1");
  });

  it("prefers an explicit public base over both derived forms", () => {
    expect(voiceSocketUrl({ ...session, wsBase: "wss://voice.example.com/" }, https))
      .toBe("wss://voice.example.com/ws/voice/vs-1");
    expect(voiceSocketUrl({ ...session, wsBase: "ws://10.0.0.5:9002" }, http))
      .toBe("ws://10.0.0.5:9002/ws/voice/vs-1");
  });

  it("treats an empty or blank base as unset", () => {
    for (const wsBase of ["", "   "]) {
      expect(voiceSocketUrl({ ...session, wsBase }, https))
        .toBe("wss://app.example.com/ws/voice/vs-1");
    }
  });
});
