/* Latency-filler cues sounded chopped in the browser even though the bytes
   arrived intact. The scheduler re-anchored every chunk at
   `currentTime + lead`, so whenever chunks arrived slightly slower than they
   play — exactly how fillers are paced (real time, no pre-fill) — it inserted
   a fresh silent gap on EVERY chunk.

   Measured from a real capture: a 0.92 s cue delivered in 46 chunks produced
   46 gaps totalling ~118 ms of silence. These tests pin the continuity, the
   preserved initial lead, and that response audio is unaffected. */
import { describe, expect, it } from "vitest";
import { PcmPlaybackQueue } from "./voiceClient";

class Ctx {
  currentTime = 0;
  constructor(readonly sampleRate = 24000) {}
  createBuffer(_c: number, length: number, rate: number) {
    return { length, sampleRate: rate, copyToChannel() {} } as unknown as AudioBuffer;
  }
  createBufferSource() {
    const node = {
      buffer: null as AudioBuffer | null,
      onended: null as (() => void) | null,
      startedAt: -1,
      connect() {}, disconnect() {},
      start(t: number) { node.startedAt = t; },
      stop() {},
    };
    return node as unknown as AudioBufferSourceNode;
  }
  createGain() {
    return { gain: { setValueAtTime() {}, linearRampToValueAtTime() {} },
             connect() {}, disconnect() {} } as unknown as GainNode;
  }
}

const CHUNK_MS = 20;
const RATE = 24000;
const chunk = () => new ArrayBuffer((RATE * CHUNK_MS) / 1000 * 2);

/** Feed `count` chunks arriving every `arrivalMs`, return the scheduled
    start times and the silent gaps between consecutive chunks. */
function feed(count: number, arrivalMs: number, owner?: string) {
  const ctx = new Ctx();
  const q = new PcmPlaybackQueue(ctx as never, RATE);
  const starts: number[] = [];
  let playhead = 0;
  const gaps: number[] = [];
  for (let i = 0; i < count; i++) {
    ctx.currentTime = (i * arrivalMs) / 1000;
    const before = playhead;
    q.enqueue(chunk(), owner);
    // Mirror the queue's own bookkeeping to observe the schedule.
    const scheduled = (q as unknown as { playhead: number }).playhead;
    const startAt = scheduled - CHUNK_MS / 1000;
    starts.push(startAt);
    if (i > 0 && startAt > before + 1e-9) gaps.push((startAt - before) * 1000);
    playhead = scheduled;
  }
  return { starts, gaps };
}

describe("filler playback continuity", () => {
  it("inserts no per-chunk gaps when chunks arrive slower than they play", () => {
    // 21 ms arrivals for 20 ms chunks — the real measured filler cadence.
    const { gaps } = feed(46, 21, "owner-1");
    expect(gaps.length).toBeLessThanOrEqual(3);
    const total = gaps.reduce((a, b) => a + b, 0);
    expect(total).toBeLessThan(60);   // was ~118 ms across 46 gaps
  });

  it("still applies the initial lead so the first chunk is not scheduled at zero", () => {
    const { starts } = feed(3, 21, "owner-1");
    expect(starts[0]).toBeCloseTo(0.04, 5);
  });

  it("re-anchors once when the playhead genuinely falls behind", () => {
    // A long stall must not be swallowed: the stream re-anchors with the lead.
    const ctx = new Ctx();
    const q = new PcmPlaybackQueue(ctx as never, RATE);
    q.enqueue(chunk(), "o");
    ctx.currentTime = 5;              // huge stall, playhead long since passed
    q.enqueue(chunk(), "o");
    expect((q as unknown as { playhead: number }).playhead)
      .toBeCloseTo(5 + 0.04 + 0.02, 5);
  });

  it("leaves response audio scheduling unchanged", () => {
    const { gaps } = feed(20, 21);    // no owner => response PCM
    expect(gaps.length).toBeLessThanOrEqual(3);
  });
});
