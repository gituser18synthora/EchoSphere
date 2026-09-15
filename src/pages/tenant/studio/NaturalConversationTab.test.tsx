import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import NaturalConversationTab from "./NaturalConversationTab";
import * as api from "@/services/api";
import type { HumanSpeechEffectiveSettings, HumanSpeechSources, VoiceBot, VoiceSettings } from "@/types/domain";

vi.mock("@/services/api", () => ({
  getVoiceSettings: vi.fn(), saveVoiceSettings: vi.fn(), getNaturalConversationAudio: vi.fn(),
  naturalConversationClipUrl: (botId: string, id: string) => `/clip/${botId}/${id}`,
  naturalConversationCueUrl: (botId: string, language: string, kind: string, id: string) => `/cue/${botId}/${language}/${kind}/${id}`,
}));
const AUDIO_CATALOG = {
  sampleRate: 24000,
  kinds: [
    { id: "breath", label: "Soft breath" }, { id: "inhale", label: "Short inhale" },
    { id: "exhale", label: "Short exhale" }, { id: "inhale_exhale", label: "Inhale-exhale" },
  ],
  cueKinds: [{ id: "hmm", label: "Thinking cue (long wait)" }, { id: "wait", label: "Spoken wait cue (very long wait)" }],
  genders: ["male", "female", "neutral"],
  voices: [{ language: "hi-IN", voiceName: "Shubh", gender: "male", provider: "sarvam", model: "bulbul:v3", voice: "shubh" }],
  clips: Object.fromEntries(["breath", "inhale", "exhale", "inhale_exhale"].map((kind) => [kind, Object.fromEntries(
    ["male", "female", "neutral"].map((gender) => [gender, [1, 2].map((n) => ({
      id: `synth:${kind}:${gender}:${n}`, label: `Synthesized ${n}`, source: "synthesized", kind, gender, durationMs: 800,
    }))]),
  )])),
  cues: { hi: { language: "hi-IN", options: {
    hmm: [{ id: "hmm", text: "Hmm…", ready: true }, { id: "hoon", text: "हूँ…", ready: false }, { id: "achha", text: "अच्छा…", ready: false }],
    wait: [{ id: "ek_second", text: "एक सेकंड…", ready: true }],
  }, defaultSelection: { primary: "hmm", alternates: ["hoon"] } } },
  effective: { latencyFillerKind: "breath", fillerAudioSelection: {}, latencyFillerCueSelection: {} },
};

const { hasPermission, toast } = vi.hoisted(() => ({ hasPermission: vi.fn(), toast: vi.fn() }));
vi.mock("@/state/AppContext", () => ({ useApp: () => ({ hasPermission, toast }) }));

const BOT = { id: "bot_1", status: "published" } as VoiceBot;
const inherited: HumanSpeechEffectiveSettings = {
  enabled: true, thinking_fillers: true, acknowledgements: true, backchannels: true,
  prosody_variation: true, gender_agreement: true, micro_pauses: true, self_correction: false,
  latency_fillers: true, sentence_breaths: true, latency_filler_ladder: true, adaptive_latency_cues: false,
  thinking_filler_probability: 0.25, acknowledgement_probability: 0.4,
  tool_ack_probability: 0.9, backchannel_probability: 0.35, micro_pause_probability: 0.45,
  self_correction_probability: 0.01, sentence_breath_probability: 0.2,
  min_long_turn_for_backchannel_ms: 4000, min_gap_between_backchannels_ms: 8000,
  max_backchannels_per_call: 4, latency_filler_delay_ms: 1500,
  latency_filler_hmm_ms: 3500, latency_filler_spoken_ms: 5000,
};
const inheritedSources = Object.fromEntries(Object.keys(inherited).map((key) => [key, "tenant"])) as HumanSpeechSources;
const SETTINGS = {
  botId: BOT.id, speed: 1.2, pauseMs: 350, ttsProvider: "sarvam", ttsVoice: "shubh",
  humanSpeech: { thinking_fillers: false },
  humanSpeechInherited: inherited, humanSpeechInheritedSources: inheritedSources,
  humanSpeechEffective: { ...inherited, thinking_fillers: false },
  humanSpeechSources: { ...inheritedSources, thinking_fillers: "bot" },
} as VoiceSettings;

const saveButton = () => screen.getByRole("button", { name: "Save natural conversation settings" });
const thinking = () => screen.getByRole("switch", { name: "Thinking fillers" });
const renderTab = (props: Partial<Parameters<typeof NaturalConversationTab>[0]> = {}) =>
  render(<MemoryRouter><NaturalConversationTab bot={BOT} {...props} /></MemoryRouter>);

describe("NaturalConversationTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    hasPermission.mockReturnValue(true);
    vi.mocked(api.getVoiceSettings).mockResolvedValue(SETTINGS);
    vi.mocked(api.getNaturalConversationAudio).mockResolvedValue(AUDIO_CATALOG as never);
    vi.mocked(api.saveVoiceSettings).mockImplementation(async (_botId, body) => ({
      settings: { ...SETTINGS, humanSpeech: body.humanSpeech ?? {} }, warnings: [],
    }));
  });

  it("saves only explicit human speech overrides and preserves unknown fields", async () => {
    const user = userEvent.setup();
    vi.mocked(api.getVoiceSettings).mockResolvedValue({
      ...SETTINGS, humanSpeech: { thinking_fillers: false, future_delivery_control: 0.7 },
    } as VoiceSettings);
    renderTab();
    await screen.findByRole("switch", { name: "Thinking fillers" });
    expect(saveButton()).toBeDisabled();
    expect(screen.getByText(/Saved changes apply to new calls for published bots/)).toBeInTheDocument();
    await user.click(screen.getByRole("switch", { name: "Acknowledgements" }));
    await user.click(saveButton());
    await waitFor(() => expect(api.saveVoiceSettings).toHaveBeenCalledWith(BOT.id, {
      humanSpeech: { thinking_fillers: false, future_delivery_control: 0.7, acknowledgements: false },
    }));
    expect(await screen.findByText("Natural conversation settings saved")).toBeInTheDocument();
    expect(api.getVoiceSettings).toHaveBeenCalledTimes(1);
    expect(saveButton()).toBeDisabled();
  });

  it("Inherit removes a saved override and uses the tenant value", async () => {
    const user = userEvent.setup();
    renderTab();
    await screen.findByRole("switch", { name: "Thinking fillers" });
    const card = thinking().closest(".card-pad-sm") as HTMLElement;
    expect(thinking()).toHaveAttribute("aria-checked", "false");
    await user.click(within(card).getByRole("button", { name: "Inherit" }));
    expect(thinking()).toHaveAttribute("aria-checked", "true");
    expect(within(card).getByText("Effective: On · source: tenant")).toBeInTheDocument();
    await user.click(saveButton());
    await waitFor(() => expect(api.saveVoiceSettings).toHaveBeenCalledWith(BOT.id, { humanSpeech: {} }));
  });

  it("failed saves retain edits and provider validation errors, then discard restores saved values", async () => {
    const user = userEvent.setup();
    vi.mocked(api.saveVoiceSettings).mockRejectedValue(Object.assign(new Error("Configuration is invalid"), {
      errors: ["The saved TTS model is unavailable. Choose a replacement in Voice."],
    }));
    renderTab();
    await screen.findByRole("switch", { name: "Thinking fillers" });
    await user.click(thinking());
    await user.click(saveButton());
    expect(await screen.findByRole("alert")).toHaveTextContent("The saved TTS model is unavailable");
    expect(thinking()).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    expect(saveButton()).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(thinking()).toHaveAttribute("aria-checked", "false");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(saveButton()).toBeDisabled();
  });

  it("uses the returned save snapshot for discard and displays save warnings", async () => {
    const user = userEvent.setup();
    vi.mocked(api.saveVoiceSettings).mockResolvedValue({
      settings: { ...SETTINGS, humanSpeech: { acknowledgements: false } },
      warnings: ["The fallback provider has no credentials."],
    });
    renderTab();
    await screen.findByRole("switch", { name: "Thinking fillers" });
    await user.click(thinking());
    await user.click(saveButton());
    expect(await screen.findByText("Saved with warnings")).toBeInTheDocument();
    expect(screen.getByText("The fallback provider has no credentials.")).toBeInTheDocument();
    expect(thinking()).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("switch", { name: "Acknowledgements" })).toHaveAttribute("aria-checked", "false");
    await user.click(thinking());
    expect(screen.queryByText("Saved with warnings")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(thinking()).toHaveAttribute("aria-checked", "true");
    expect(saveButton()).toBeDisabled();
    expect(api.getVoiceSettings).toHaveBeenCalledTimes(1);
  });

  it("keeps settings read-only without either management permission", async () => {
    hasPermission.mockReturnValue(false);
    renderTab();
    await screen.findByRole("switch", { name: "Thinking fillers" });
    expect(screen.getByText(/You can view these settings/)).toBeInTheDocument();
    screen.getAllByRole("switch").forEach((toggle) => expect(toggle).toBeDisabled());
    expect(screen.getByRole("button", { name: "Clear all overrides" })).toBeDisabled();
    expect(saveButton()).toBeDisabled();
    expect(api.saveVoiceSettings).not.toHaveBeenCalled();
  });

  it("permits editing with bots.manage alone", async () => {
    hasPermission.mockImplementation((permission) => permission === "bots.manage");
    renderTab();
    expect(await screen.findByRole("switch", { name: "Thinking fillers" })).toBeEnabled();
  });

  it.each([undefined, {}])("offers retry when inheritance metadata is unavailable (%s)", async (metadata) => {
    const user = userEvent.setup();
    vi.mocked(api.getVoiceSettings).mockResolvedValueOnce({
      ...SETTINGS, humanSpeechInherited: metadata,
    } as VoiceSettings);
    renderTab();
    expect(await screen.findByRole("alert")).toHaveTextContent("could not load their inherited values");
    expect(screen.queryByTestId("human-speech-bot")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("switch", { name: "Thinking fillers" })).toBeInTheDocument();
    expect(api.getVoiceSettings).toHaveBeenCalledTimes(2);
  });

  it("reports unsaved edits and guards unloading only while dirty", async () => {
    const user = userEvent.setup();
    const onDirtyChange = vi.fn();
    const view = renderTab({ onDirtyChange });
    await screen.findByRole("switch", { name: "Thinking fillers" });
    const cleanEvent = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(cleanEvent);
    expect(cleanEvent.defaultPrevented).toBe(false);
    await user.click(thinking());
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    const dirtyEvent = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(dirtyEvent);
    expect(dirtyEvent.defaultPrevented).toBe(true);
    view.unmount();
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
    const unmountedEvent = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unmountedEvent);
    expect(unmountedEvent.defaultPrevented).toBe(false);
  });

  it("disables editing throughout a pending save", async () => {
    const user = userEvent.setup();
    const onSavingChange = vi.fn();
    let resolveSave!: (value: Awaited<ReturnType<typeof api.saveVoiceSettings>>) => void;
    vi.mocked(api.saveVoiceSettings).mockImplementation(() => new Promise((resolve) => { resolveSave = resolve; }));
    renderTab({ onSavingChange });
    await screen.findByRole("switch", { name: "Thinking fillers" });
    await user.click(thinking());
    await user.click(saveButton());
    expect(thinking()).toBeDisabled();
    expect(onSavingChange).toHaveBeenLastCalledWith(true);
    expect(screen.getByRole("button", { name: "Discard changes" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Test call" })).toBeDisabled();
    await act(async () => resolveSave({ settings: { ...SETTINGS, humanSpeech: { thinking_fillers: true } }, warnings: [] }));
    expect(thinking()).toBeEnabled();
    expect(onSavingChange).toHaveBeenLastCalledWith(false);
    expect(saveButton()).toBeDisabled();
  });

  it("uses the Studio navigation callback for test calls and voice settings", async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    renderTab({ onNavigate });
    await screen.findByRole("switch", { name: "Thinking fillers" });
    await user.click(screen.getByRole("button", { name: "Test call" }));
    expect(onNavigate).toHaveBeenLastCalledWith("testing");
    await user.click(screen.getByRole("button", { name: "Voice settings" }));
    expect(onNavigate).toHaveBeenLastCalledWith("voice");
  });

  it("validates advanced numeric overrides before saving", async () => {
    const user = userEvent.setup();
    renderTab();
    await screen.findByRole("switch", { name: "Thinking fillers" });
    await user.click(screen.getByText(/^Advanced/));
    fireEvent.change(screen.getByRole("spinbutton", { name: "Backchannel probability" }), { target: { value: "1.5" } });
    await user.click(saveButton());
    expect(await screen.findByRole("alert")).toHaveTextContent("Backchannel probability must be between 0 and 1.");
    expect(api.saveVoiceSettings).not.toHaveBeenCalled();
  });
});
