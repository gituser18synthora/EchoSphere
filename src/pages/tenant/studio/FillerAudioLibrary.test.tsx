/* Filler audio library: catalog rendering, gender filter, authenticated
   preview playback, primary/alternate selection written into the sparse
   humanSpeech override, and inherit/clear paths. */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import FillerAudioLibrary, { choiceIds, withClipChoice } from "./FillerAudioLibrary";
import * as api from "@/services/api";
import type { HumanSpeechEffectiveSettings, HumanSpeechSettings, NaturalConversationAudio } from "@/types/domain";

vi.mock("@/services/api", () => ({
  getNaturalConversationAudio: vi.fn(),
  naturalConversationClipUrl: (botId: string, id: string) => `/api/v1/bots/${botId}/natural-conversation/audio/clip?id=${id}`,
  naturalConversationCueUrl: (botId: string, language: string, kind: string, id: string) =>
    `/api/v1/bots/${botId}/natural-conversation/audio/cue?language=${language}&kind=${kind}&id=${id}`,
}));
vi.mock("@/services/http", () => ({ getToken: () => "jwt-token" }));

const clip = (kind: string, gender: string, n: number, source = "synthesized") => ({
  id: `${source === "recording" ? "file" : "synth"}:${kind}:${gender}:${n}`,
  label: source === "recording" ? `Studio take ${n}` : `Synthesized ${n}`,
  source, kind, gender, durationMs: 700 + 100 * n,
});
const CATALOG = {
  sampleRate: 24000,
  kinds: [
    { id: "breath", label: "Soft breath" }, { id: "inhale", label: "Short inhale" },
    { id: "exhale", label: "Short exhale" }, { id: "inhale_exhale", label: "Inhale-exhale" },
  ],
  cueKinds: [{ id: "hmm", label: "Thinking cue (long wait)" }, { id: "wait", label: "Spoken wait cue (very long wait)" }],
  genders: ["male", "female", "neutral"],
  voices: [{ language: "hi-IN", voiceName: "Shubh", gender: "male", provider: "sarvam", model: "bulbul:v3", voice: "shubh" }],
  clips: Object.fromEntries(["breath", "inhale", "exhale", "inhale_exhale"].map((kind) => [kind, {
    male: [clip(kind, "male", 1, "recording"), clip(kind, "male", 2), clip(kind, "male", 3)],
    female: [clip(kind, "female", 1), clip(kind, "female", 2)],
    neutral: [clip(kind, "neutral", 1)],
  }])),
  cues: { hi: { language: "hi-IN", options: {
    hmm: [{ id: "hmm", text: "हम्म…", ready: true }, { id: "hoon", text: "हूँ…", ready: false }, { id: "achha", text: "अच्छा…", ready: false }],
    wait: [{ id: "ek_second", text: "एक सेकंड…", ready: true }],
  }, defaultSelection: { primary: "hmm", alternates: ["hoon"] } } },
  effective: { latencyFillerKind: "breath", fillerAudioSelection: {}, latencyFillerCueSelection: {} },
} as unknown as NaturalConversationAudio;

const inherited = {
  latency_filler_kind: "breath", filler_audio_selection: {}, latency_filler_cue_selection: {},
} as HumanSpeechEffectiveSettings;

function renderLibrary(override: HumanSpeechSettings = {}, onChange = vi.fn(), disabled = false) {
  render(<FillerAudioLibrary botId="bot_1" override={override} inherited={inherited} disabled={disabled} onChange={onChange} />);
  return onChange;
}

const playMock = vi.fn().mockResolvedValue(undefined);

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getNaturalConversationAudio).mockResolvedValue(CATALOG);
  Object.defineProperty(window.HTMLMediaElement.prototype, "play", { configurable: true, value: playMock });
  Object.defineProperty(window.HTMLMediaElement.prototype, "pause", { configurable: true, value: vi.fn() });
  Object.defineProperty(window.HTMLMediaElement.prototype, "load", { configurable: true, value: vi.fn() });
  (globalThis.URL as unknown as { createObjectURL: unknown }).createObjectURL = vi.fn(() => "blob:preview");
  (globalThis.URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = vi.fn();
  globalThis.fetch = vi.fn().mockResolvedValue({
    ok: true, status: 200,
    headers: { get: () => "audio/wav" },
    blob: async () => new Blob([new Uint8Array(4)], { type: "audio/wav" }),
  } as unknown as Response);
});

describe("FillerAudioLibrary", () => {
  it("shows the runtime voice gender by default and lets the operator inspect the other library", async () => {
    const user = userEvent.setup();
    renderLibrary();
    await screen.findByTestId("filler-audio-library");
    expect(screen.getByText(/hi-IN: Shubh \(male\)/)).toBeInTheDocument();
    // Only male clips are listed while matching the bot voice.
    expect(screen.getByTestId("clips-breath-male")).toBeInTheDocument();
    expect(screen.queryByTestId("clips-breath-female")).not.toBeInTheDocument();
    expect(within(screen.getByTestId("clips-breath-male")).getByText("Studio take 1")).toBeInTheDocument();
    expect(screen.getByTestId("rotation-breath-male")).toHaveTextContent("No selection — all 3 clips rotate.");
    await user.click(screen.getByRole("button", { name: "Female" }));
    expect(screen.getByTestId("clips-breath-female")).toBeInTheDocument();
    expect(screen.queryByTestId("clips-breath-male")).not.toBeInTheDocument();
  });

  it("previews the exact clip with the JWT and reports what is playing", async () => {
    const user = userEvent.setup();
    renderLibrary();
    await screen.findByTestId("filler-audio-library");
    await user.click(screen.getByRole("button", { name: "Play Short exhale · Male · Synthesized 2" }));
    await waitFor(() => expect(playMock).toHaveBeenCalledTimes(1));
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/api/v1/bots/bot_1/natural-conversation/audio/clip?id=synth:exhale:male:2",
      { headers: { Authorization: "Bearer jwt-token" } },
    );
    await waitFor(() => expect(screen.getByTestId("now-playing")).toHaveTextContent("Now playing: Short exhale · Male · Synthesized 2"));
    await user.click(screen.getByRole("button", { name: "Stop Short exhale · Male · Synthesized 2" }));
    expect(screen.getByTestId("now-playing")).toHaveTextContent("Nothing playing.");
  });

  it("surfaces a failed preview instead of playing silence", async () => {
    const user = userEvent.setup();
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false, status: 502, headers: { get: () => "application/json" },
      json: async () => ({ error: { message: "This cue could not be rendered for the bot's voice." } }),
    } as unknown as Response);
    renderLibrary();
    await screen.findByTestId("filler-audio-library");
    await user.click(screen.getByRole("button", { name: "Play Thinking cue (long wait) · hi-IN · अच्छा…" }));
    expect(await screen.findByText("This cue could not be rendered for the bot's voice.")).toBeInTheDocument();
    expect(playMock).not.toHaveBeenCalled();
  });

  it("writes the gap sound and clip primary/alternates into the sparse override", async () => {
    const user = userEvent.setup();
    const onChange = renderLibrary();
    await screen.findByTestId("filler-audio-library");
    await user.click(screen.getByRole("radio", { name: "Gap sound: Short exhale" }));
    expect(onChange).toHaveBeenLastCalledWith({ latency_filler_kind: "exhale" });
    await user.click(screen.getByRole("radio", { name: "Primary: Short exhale · Male · Synthesized 2" }));
    expect(onChange).toHaveBeenLastCalledWith({
      filler_audio_selection: { exhale: { male: { primary: "synth:exhale:male:2", alternates: [] } } },
    });
  });

  it("keeps existing selections when adding an alternate and clears back to all clips", async () => {
    const user = userEvent.setup();
    const override: HumanSpeechSettings = {
      filler_audio_selection: { exhale: { male: { primary: "synth:exhale:male:2", alternates: [] } } },
    };
    const onChange = renderLibrary(override);
    await screen.findByTestId("filler-audio-library");
    expect(screen.getByTestId("rotation-exhale-male")).toHaveTextContent("Always plays: Synthesized 2.");
    // The primary cannot also be an alternate.
    expect(screen.getByRole("checkbox", { name: "Alternate: Short exhale · Male · Synthesized 2" })).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: "Alternate: Short exhale · Male · Studio take 1" }));
    expect(onChange).toHaveBeenLastCalledWith({
      filler_audio_selection: { exhale: { male: { primary: "synth:exhale:male:2", alternates: ["file:exhale:male:1"] } } },
    });
    await user.click(within(screen.getByTestId("clips-exhale-male")).getByRole("button", { name: "Use all clips" }));
    // The bot explicitly overrides to "nothing selected" (not inheritance).
    expect(onChange).toHaveBeenLastCalledWith({ filler_audio_selection: {} });
    await user.click(screen.getByRole("button", { name: "Inherit tenant/platform selection" }));
    expect(onChange).toHaveBeenLastCalledWith({});
  });

  it("shows the language default cue rotation and lets the bot pick its own cues", async () => {
    const user = userEvent.setup();
    const onChange = renderLibrary();
    await screen.findByTestId("filler-audio-library");
    expect(screen.getByTestId("cue-rotation-hi")).toHaveTextContent("Chosen by context among: हम्म…, हूँ…");
    expect(screen.getByRole("radio", { name: "Neutral default: Thinking cue (long wait) · hi-IN · हम्म…" })).toBeChecked();
    await user.click(screen.getByRole("radio", { name: "Neutral default: Thinking cue (long wait) · hi-IN · अच्छा…" }));
    expect(onChange).toHaveBeenLastCalledWith({
      latency_filler_cue_selection: { hi: { primary: "achha", alternates: ["hoon"] } },
    });
    // The spoken wait cue is fixed: preview only, no selection controls.
    expect(screen.queryByRole("radio", { name: /Spoken wait cue/ })).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Allowed: Thinking cue (long wait) · hi-IN · हूँ…" })).toBeChecked();
    expect(screen.getByRole("button", { name: "Play Spoken wait cue (very long wait) · hi-IN · एक सेकंड…" })).toBeInTheDocument();
  });

  it("is read-only when disabled and reports a failed catalog load with retry", async () => {
    renderLibrary({}, vi.fn(), true);
    await screen.findByTestId("filler-audio-library");
    expect(screen.getByRole("radio", { name: "Gap sound: Short inhale" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "Primary: Soft breath · Male · Studio take 1" })).toBeDisabled();
    // Playback stays available for inspection.
    expect(screen.getByRole("button", { name: "Play Soft breath · Male · Studio take 1" })).toBeEnabled();
  });

  it("retries a failed catalog load", async () => {
    const user = userEvent.setup();
    vi.mocked(api.getNaturalConversationAudio).mockRejectedValueOnce(new Error("boom"));
    renderLibrary();
    expect(await screen.findByText("boom")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByTestId("filler-audio-library");
  });
});

describe("selection helpers", () => {
  it("orders ids primary first without duplicates", () => {
    expect(choiceIds({ primary: "a", alternates: ["b", "a", "c", "b"] })).toEqual(["a", "b", "c"]);
    expect(choiceIds(null)).toEqual([]);
  });

  it("prunes empty branches", () => {
    const one = withClipChoice({}, "breath", "male", { primary: "x", alternates: [] });
    expect(one).toEqual({ breath: { male: { primary: "x", alternates: [] } } });
    expect(withClipChoice(one, "breath", "male", null)).toEqual({});
    expect(withClipChoice(one, "breath", "female", { alternates: ["y"] })).toEqual({
      breath: { male: { primary: "x", alternates: [] }, female: { primary: undefined, alternates: ["y"] } },
    });
  });
});
