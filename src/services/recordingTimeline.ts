import type { ConversationRecording, TraceStep } from "@/types/domain";

/** Displayed accepted/final marker; independent of the playback anchor. */
export function recordingMarkerOffset(
  turn: TraceStep, recording: ConversationRecording, conversationStartedAt: string,
): number | null {
  if (!turn.at) return null;
  const origin = Date.parse(recording.startedAt ?? conversationStartedAt);
  const seconds = (Date.parse(turn.at) - origin) / 1000;
  if (!Number.isFinite(seconds) || seconds < 0) return null;
  if (recording.durationSec > 0 && seconds >= recording.durationSec) return null;
  return seconds;
}

/** Caller audio starts before STT finalization. Never seek from its final time. */
export function recordingOffset(
  turn: TraceStep, recording: ConversationRecording, conversationStartedAt: string, transcript: TraceStep[] = [],
): number | null {
  if (turn.speaker !== "user") return recordingMarkerOffset(turn, recording, conversationStartedAt);
  const origin = Date.parse(recording.startedAt ?? conversationStartedAt);
  if (!Number.isFinite(origin)) return null;
  const accepted = Date.parse(turn.at ?? "");
  if (Number.isFinite(accepted) && accepted < origin) return null;
  const offset = (timestamp: number): number | null => {
    if (!Number.isFinite(timestamp) || (Number.isFinite(accepted) && timestamp > accepted)) return null;
    const seconds = Math.max(0, (timestamp - origin) / 1000);
    return recording.durationSec > 0 && seconds >= recording.durationSec ? null : seconds;
  };
  for (const candidate of [turn.audioStartedAt, turn.playbackAt]) {
    if (candidate) {
      const seconds = offset(Date.parse(candidate));
      if (seconds !== null) return seconds;
    }
  }
  if (recordingMarkerOffset(turn, recording, conversationStartedAt) === null) return null;
  // Very old calls have neither VAD timing nor a bot-audio-end event. Include
  // the previous bot's context so the caller's opening words are not skipped.
  const precedingBot = transcript
    .filter((item) => item.speaker === "bot")
    .map((item) => Date.parse(item.at ?? ""))
    .filter((timestamp) => Number.isFinite(timestamp) && timestamp < accepted);
  return precedingBot.length ? offset(Math.max(...precedingBot)) : 0;
}

export function formatRecordingTime(seconds: number): string {
  const hundredths = Math.floor(Math.max(0, seconds) * 100 + 0.00001);
  return `${String(Math.floor(hundredths / 6000)).padStart(2, "0")}:${String(Math.floor(hundredths / 100) % 60).padStart(2, "0")}.${String(hundredths % 100).padStart(2, "0")}`;
}
