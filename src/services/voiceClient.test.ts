/* Voice client: playback scheduling, sample-rate handling, resampling and
   the stale-audio gate. AudioContext is faked — the queue's scheduling math
   (sequential playhead, no overlap, cancellation) is what's under test. */
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  PcmPlaybackQueue,
  VoiceClient,
  downsampleLinear,
  type PlaybackContextLike,
} from "./voiceClient";

/* ---------- fakes ---------- */

class FakeSource {
  buffer: { duration: number; length: number } | null = null;
  started: number[] = [];
  stopped = false;
  onended: (() => void) | null = null;
  connect = vi.fn();
  start(at: number) {
    this.started.push(at);
  }
  stop() {
    this.stopped = true;
  }
}

function fakeContext(_sampleRate: number) {
  const sources: FakeSource[] = [];
  const ctx = {
    currentTime: 0,
    destination: {} as AudioNode,
    createBuffer(_ch: number, length: number, rate: number) {
      return {
        duration: length / rate,
        length,
        copyToChannel: vi.fn(),
      } as unknown as AudioBuffer;
    },
    createBufferSource() {
      const src = new FakeSource();
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
  return { client, queue, sources: fake.sources, events };
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
