import { describe, expect, it } from "vitest";
import { formatRecordingTime, recordingMarkerOffset, recordingOffset } from "./recordingTimeline";
import type { ConversationRecording, TraceStep } from "@/types/domain";

const recording: ConversationRecording = {
  url: "/recording", mimeType: "audio/wav", sizeBytes: 1000, durationSec: 300,
  startedAt: "2026-11-17T10:00:10Z", timingSource: "recording",
};
const turn = (at?: string) => ({ turn: 1, speaker: "user", text: "Hello", at, audioStartedAt: at }) as TraceStep;

describe("Recording timeline", () => {
  it("uses the audio origin rather than ringing/session time, with timezone support", () => {
    expect(recordingOffset(turn("2026-11-17T15:33:22+05:30"), recording, "2026-11-17T10:00:00Z")).toBe(192);
    expect(formatRecordingTime(192)).toBe("03:12.00");
    expect(formatRecordingTime(154.25)).toBe("02:34.25");
    expect(formatRecordingTime(3661)).toBe("61:01.00");
  });

  it("supports legacy recordings without inventing missing turn times", () => {
    const legacy = { ...recording, startedAt: undefined };
    expect(recordingOffset(turn("2026-11-17T10:00:00Z"), legacy, "2026-11-17T10:00:00Z")).toBe(0);
    for (const at of [undefined, "invalid", "2026-11-17T10:00:09Z", "2026-11-17T10:05:10Z"]) {
      expect(recordingOffset(turn(at), recording, "2026-11-17T10:00:00Z")).toBeNull();
    }
  });

  it("keeps completion display unchanged while seeking from caller speech onset", () => {
    const caller = { ...turn("2026-11-17T10:00:30Z"), audioStartedAt: "2026-11-17T10:00:21Z" };
    expect(recordingMarkerOffset(caller, recording, "")).toBe(20);
    expect(recordingOffset(caller, recording, "")).toBe(11);
    // Speech can overlap the bot: an observed onset takes priority over end.
    expect(recordingOffset({ ...caller, playbackAt: "2026-11-17T10:00:25Z" }, recording, "")).toBe(11);
  });

  it("uses bot audio end, then earlier bot context, for calls without speech timing", () => {
    const caller = { ...turn("2026-11-17T10:00:30Z"), audioStartedAt: undefined };
    expect(recordingOffset({ ...caller, playbackAt: "2026-11-17T10:00:18Z" }, recording, "")).toBe(8);
    const bot = { ...turn("2026-11-17T10:00:12Z"), speaker: "bot" as const };
    const futureBot = { ...bot, at: "2026-11-17T10:00:40Z" };
    expect(recordingOffset(caller, recording, "", [futureBot, bot])).toBe(2);
    expect(recordingOffset(caller, recording, "")).toBe(0);
    expect(recordingMarkerOffset(caller, recording, "")).toBe(20);
  });

  it("clamps speech beginning before recording and rejects future/invalid anchors", () => {
    const caller = turn("2026-11-17T10:00:30Z");
    expect(recordingOffset({ ...caller, audioStartedAt: "2026-11-17T10:00:09Z" }, recording, "")).toBe(0);
    expect(recordingOffset({ ...caller, audioStartedAt: "2026-11-17T10:00:31Z", playbackAt: "2026-11-17T10:00:16Z" }, recording, "")).toBe(6);
    expect(recordingOffset({ ...caller, audioStartedAt: "invalid", playbackAt: "invalid" }, recording, "")).toBe(0);
  });
});
