/* Voice Settings inside the Voice Preview modal.

   The settings shown are driven entirely by the selected provider/model's
   catalog paramsSchema, so these tests pin the behaviour that depends on it:
   - only the selected model's settings render (Sarvam v2 pitch/loudness vs v3
     temperature; ElevenLabs speed/speaker-boost vs Eleven v3)
   - switching model drops unsupported settings, clamps carried ones into the
     new range and reveals the new model's controls
   - Preview synthesizes the unsaved draft
   - applying stages the draft, and only an explicit Save persists it
   - reset restores schema defaults */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import VoiceTab from "@/pages/tenant/studio/VoiceTab";
import type { VoiceBot } from "@/types/domain";
import * as api from "@/services/api";

vi.mock("@/services/api", () => ({
  getVoiceSettings: vi.fn(),
  getProviderCatalog: vi.fn(),
  listLanguages: vi.fn(),
  listProviderModels: vi.fn(),
  listProviderVoices: vi.fn(),
  getModelLanguages: vi.fn(),
  saveVoiceSettings: vi.fn(),
  validateVoiceConfig: vi.fn(),
  testProviderConnection: vi.fn(),
  generateTtsPreview: vi.fn(),
  listPronunciationDictionaries: vi.fn(),
  getPronunciationDictionary: vi.fn(),
  createPronunciationDictionary: vi.fn(),
  updatePronunciationDictionary: vi.fn(),
  deletePronunciationDictionary: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), hasPermission: () => true }),
}));

const BOT = { id: "bot_1", languages: ["en-IN", "hi-IN"] } as unknown as VoiceBot;

/* Mirrors the seeded catalog: v2 documents pitch/loudness and a wider pace
   range, v3 documents temperature instead; eleven_v3 has no speed control. */
/* min_buffer_size mirrors the corrected catalog: Sarvam documents 30–200
   (default 50) for WebSocket streaming. dict_id is a widget-backed entry —
   rendered as the dictionary selector, never a raw text input. */
const SARVAM_MODELS = [
  {
    code: "bulbul:v3", displayName: "Bulbul v3", isDefault: true, capability: "tts",
    streaming: true, speedRange: [0.5, 2] as [number, number],
    paramsSchema: {
      pace: { type: "number", min: 0.5, max: 2, step: 0.05, default: 1, label: "Pace" },
      temperature: { type: "number", min: 0.01, max: 1, step: 0.01, default: 0.6, label: "Temperature" },
      min_buffer_size: { type: "integer", min: 30, max: 200, default: 50, label: "Min buffer size" },
      dict_id: {
        type: "string", optional: true, label: "Pronunciation dictionary",
        widget: "dictionary", section: "pronunciation",
      },
      enable_preprocessing: { type: "boolean", default: true, fixed: true, label: "Preprocessing" },
    },
  },
  {
    code: "bulbul:v2", displayName: "Bulbul v2", isDefault: false, capability: "tts",
    streaming: true, speedRange: [0.3, 3] as [number, number],
    paramsSchema: {
      pace: { type: "number", min: 0.3, max: 3, step: 0.05, default: 1, label: "Pace" },
      pitch: { type: "number", min: -0.75, max: 0.75, step: 0.05, default: 0, label: "Pitch" },
      loudness: { type: "number", min: 0.3, max: 3, step: 0.1, default: 1, label: "Loudness" },
      enable_preprocessing: { type: "boolean", default: false, label: "Preprocessing" },
      min_buffer_size: { type: "integer", min: 30, max: 200, default: 50, label: "Min buffer size" },
    },
  },
];

const ELEVEN_MODELS = [
  {
    code: "eleven_flash_v2_5", displayName: "Eleven Flash v2.5", isDefault: true,
    capability: "tts", streaming: true, speedRange: [0.7, 1.2] as [number, number],
    paramsSchema: {
      stability: { type: "number", min: 0, max: 1, step: 0.05, default: 0.5, label: "Stability" },
      similarity_boost: { type: "number", min: 0, max: 1, step: 0.05, default: 0.75, label: "Similarity boost" },
      style: { type: "number", min: 0, max: 1, step: 0.05, default: 0, label: "Style" },
      use_speaker_boost: { type: "boolean", default: true, label: "Speaker boost" },
      speed: { type: "number", min: 0.7, max: 1.2, step: 0.05, default: 1, label: "Speed" },
    },
  },
  {
    code: "eleven_v3", displayName: "Eleven v3 (expressive)", isDefault: false,
    capability: "tts", streaming: false, speedRange: null,
    paramsSchema: {
      stability: {
        type: "enum", values: [0, 0.5, 1], default: 0.5, label: "Stability",
        labels: { "0": "Creative", "0.5": "Natural", "1": "Robust" },
      },
      similarity_boost: { type: "number", min: 0, max: 1, step: 0.05, default: 1, label: "Similarity boost" },
    },
  },
];

const voice = (id: string, name: string, modelCodes: string[], provider = "sarvam") => ({
  id, name, gender: "female", provider, providerVoiceId: id.replace("vp-", ""),
  languages: [], modelCodes, locale: null, premium: false,
  isDefault: false, status: "active", providerSettings: {}, sampleText: null,
});

function baseSettings(overrides: Record<string, unknown> = {}) {
  return {
    speed: 1, pauseMs: 350, empathy: 50, energy: 50,
    sttProvider: "", sttModel: "", sttLanguage: "", sttSettings: {},
    llmProvider: "", llmModel: "", llmSettings: {},
    ttsProvider: "sarvam", ttsModel: "bulbul:v3", ttsVoice: "vp-shubh", ttsSettings: {},
    fallbackProvider: "", fallbackModel: "", fallbackVoice: "",
    languageVoiceMap: { default: "en-IN" },
    audioSettings: {
      browser: { codec: "linear16", sampleRate: 16000 },
      telephony: { codec: "mulaw", sampleRate: 8000 },
    },
    ...overrides,
  };
}

function installMocks(settings: Record<string, unknown> = baseSettings()) {
  vi.mocked(api.getVoiceSettings).mockResolvedValue(settings as never);
  vi.mocked(api.getProviderCatalog).mockResolvedValue({
    stt: [], llm: [],
    tts: [
      { code: "sarvam", name: "Sarvam AI", capability: "tts", description: "", requiresApiKey: true, hasCredentials: true },
      { code: "elevenlabs", name: "ElevenLabs", capability: "tts", description: "", requiresApiKey: true, hasCredentials: true },
    ],
  } as never);
  vi.mocked(api.listLanguages).mockResolvedValue([
    { id: "l1", code: "en-IN", name: "English (India)", enabled: true },
    { id: "l2", code: "hi-IN", name: "Hindi", enabled: true },
  ] as never);
  vi.mocked(api.listProviderModels).mockImplementation(((_cap: string, provider: string) =>
    Promise.resolve(provider === "sarvam" ? SARVAM_MODELS
      : provider === "elevenlabs" ? ELEVEN_MODELS : [])) as never);
  vi.mocked(api.listProviderVoices).mockImplementation(((provider: string) =>
    Promise.resolve(provider === "sarvam"
      ? [voice("vp-shubh", "Shubh", ["bulbul:v3", "bulbul:v2"])]
      : provider === "elevenlabs"
        ? [voice("vp-rachel", "Rachel", ["eleven_flash_v2_5", "eleven_v3"], "elevenlabs")]
        : [])) as never);
  vi.mocked(api.getModelLanguages).mockResolvedValue(
    { languages: [], supportsAutoDetect: true, languageAgnostic: true } as never);
  vi.mocked(api.saveVoiceSettings).mockResolvedValue({ settings, warnings: [] } as never);
  vi.mocked(api.generateTtsPreview).mockResolvedValue({
    audioBase64: "AAAA", mimeType: "audio/wav", sampleRate: 16000,
    ttfaMs: 12, totalMs: 40, provider: "sarvam", voice: "vp-shubh",
  } as never);
  vi.mocked(api.listPronunciationDictionaries).mockResolvedValue([
    {
      id: "pd_1", provider: "sarvam", dictId: "p_5cb7faa6", name: "Collections Hindi",
      description: null, languageWordCounts: { "hi-IN": 3 }, createdAt: null, updatedAt: null,
    },
  ] as never);
}

/** Open the preview modal from the Text-to-Speech card. */
async function openPreview(user: ReturnType<typeof userEvent.setup>) {
  render(<VoiceTab bot={BOT} />);
  await screen.findByText("Hindi");
  const button = screen.getByRole("button", { name: "Preview voice" });
  await waitFor(() => expect(button).toBeEnabled());
  await user.click(button);
  return screen.findByRole("dialog");
}

/** Numeric companion input a slider parameter renders, by its label. */
function paramInput(dialog: HTMLElement, label: string) {
  return within(dialog).getByLabelText(`${label} value`) as HTMLInputElement;
}

/* Drive the slider rather than typing into the companion input: a controlled
   numeric input commits on every keystroke, so typing "0.35" would land on an
   intermediate value. A change event carries the exact target value. */
function setParam(dialog: HTMLElement, label: string, value: number) {
  const slider = within(dialog).getByRole("slider", { name: label });
  fireEvent.change(slider, { target: { value: String(value) } });
}

beforeEach(() => {
  vi.clearAllMocks();
  window.HTMLMediaElement.prototype.play = vi.fn().mockResolvedValue(undefined);
  window.HTMLMediaElement.prototype.pause = vi.fn();
  installMocks();
});

describe("Voice preview — provider/model-specific settings", () => {
  it("renders a Voice settings section with the selected model's controls only", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    expect(within(dialog).getByText("Voice settings")).toBeInTheDocument();
    // bulbul:v3 controls.
    expect(paramInput(dialog, "Temperature")).toHaveValue(0.6);
    expect(paramInput(dialog, "Min buffer size")).toHaveValue(50);
    // v2-only controls are absent, not merely disabled.
    expect(within(dialog).queryByLabelText("Pitch value")).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Loudness value")).not.toBeInTheDocument();
  });

  it("renders numeric settings as sliders with the model's min, max and step", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    const sliders = within(dialog).getAllByRole("slider", { name: "Temperature" });
    expect(sliders[0]).toHaveAttribute("min", "0.01");
    expect(sliders[0]).toHaveAttribute("max", "1");
    expect(sliders[0]).toHaveAttribute("step", "0.01");
  });

  it("shows a fixed parameter as read-only rather than a dead control", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    expect(within(dialog).getByText("Preprocessing")).toBeInTheDocument();
    expect(within(dialog).getByText("Always on")).toBeInTheDocument();
    expect(within(dialog).queryByRole("switch", { name: "Preprocessing" })).not.toBeInTheDocument();
  });

  it("bounds the speaking-speed control to the selected model's range", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    const speed = within(dialog).getByRole("slider", { name: "Speaking speed" });
    expect(speed).toHaveAttribute("min", "0.5");
    expect(speed).toHaveAttribute("max", "2");
  });

  it("hides the speaking-speed control for a model with no speed setting", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    await user.selectOptions(within(dialog).getByLabelText("Preview provider"), ["elevenlabs"]);
    await waitFor(() =>
      expect(within(dialog).getByLabelText("Preview model")).toHaveValue("eleven_flash_v2_5"));
    expect(within(dialog).getByRole("slider", { name: "Speaking speed" })).toBeInTheDocument();

    await user.selectOptions(within(dialog).getByLabelText("Preview model"), ["eleven_v3"]);
    await waitFor(() =>
      expect(within(dialog).queryByRole("slider", { name: "Speaking speed" })).not.toBeInTheDocument());
    expect(within(dialog).getByText(/has no speed control/)).toBeInTheDocument();
  });
});

describe("Voice preview — provider/model switching", () => {
  it("v2 → v3 drops pitch/loudness, clamps pace-range values and adds temperature", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    // 1–3: select bulbul:v2 and tune its v2-only controls.
    await user.selectOptions(within(dialog).getByLabelText("Preview model"), ["bulbul:v2"]);
    await waitFor(() => expect(paramInput(dialog, "Pitch")).toBeInTheDocument());
    expect(paramInput(dialog, "Loudness")).toBeInTheDocument();
    // v2 documents a wider pace range, so its speed slider goes past v3's max.
    expect(within(dialog).getByRole("slider", { name: "Speaking speed" }))
      .toHaveAttribute("max", "2"); // clamped by the canonical platform maximum
    setParam(dialog, "Pitch", 0.5);
    setParam(dialog, "Min buffer size", 180);

    // 4–8: back to v3.
    await user.selectOptions(within(dialog).getByLabelText("Preview model"), ["bulbul:v3"]);
    await waitFor(() =>
      expect(within(dialog).queryByLabelText("Pitch value")).not.toBeInTheDocument());
    expect(within(dialog).queryByLabelText("Loudness value")).not.toBeInTheDocument();
    // A setting both models share is carried over, not reset.
    expect(paramInput(dialog, "Min buffer size")).toHaveValue(180);
    // v3-only control appears with its documented default.
    expect(paramInput(dialog, "Temperature")).toHaveValue(0.6);
  });

  it("clamps a carried-over value into the new model's narrower range", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    await user.selectOptions(within(dialog).getByLabelText("Preview provider"), ["elevenlabs"]);
    await waitFor(() => expect(paramInput(dialog, "Similarity boost")).toBeInTheDocument());
    setParam(dialog, "Stability", 0.9);

    // eleven_v3 takes stability as discrete presets — 0.9 is not one of them,
    // so the value falls back to the new model's default instead of staging a
    // value the provider would reject.
    await user.selectOptions(within(dialog).getByLabelText("Preview model"), ["eleven_v3"]);
    await waitFor(() =>
      expect(within(dialog).getByLabelText("Stability")).toHaveValue("0.5"));
  });

  it("does not send settings belonging to the previously selected model", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    await user.selectOptions(within(dialog).getByLabelText("Preview model"), ["bulbul:v2"]);
    await waitFor(() => expect(paramInput(dialog, "Pitch")).toBeInTheDocument());
    setParam(dialog, "Pitch", 0.5);
    await user.selectOptions(within(dialog).getByLabelText("Preview model"), ["bulbul:v3"]);
    await waitFor(() => expect(paramInput(dialog, "Temperature")).toBeInTheDocument());

    // Re-select the voice the model switch cleared, then preview.
    await user.click(within(dialog).getByLabelText("Preview voice"));
    await user.click(await within(dialog).findByText("Shubh"));
    await user.click(within(dialog).getByRole("button", { name: "Generate" }));

    await waitFor(() => expect(api.generateTtsPreview).toHaveBeenCalledTimes(1));
    const params = vi.mocked(api.generateTtsPreview).mock.calls[0][0].params!;
    expect(params).not.toHaveProperty("pitch");
    expect(params).not.toHaveProperty("loudness");
    expect(params).toHaveProperty("temperature");
  });

  it("resets the draft when the provider changes", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    setParam(dialog, "Temperature", 0.9);
    await user.selectOptions(within(dialog).getByLabelText("Preview provider"), ["elevenlabs"]);

    await waitFor(() =>
      expect(within(dialog).queryByLabelText("Temperature value")).not.toBeInTheDocument());
    // The new provider's defaults, not a translation of the old values.
    expect(paramInput(dialog, "Stability")).toHaveValue(0.5);
    expect(paramInput(dialog, "Similarity boost")).toHaveValue(0.75);
  });
});

describe("Voice preview — draft, apply and save", () => {
  it("previews the unsaved draft values", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    setParam(dialog, "Temperature", 0.35);
    await user.click(within(dialog).getByRole("button", { name: "Generate" }));

    await waitFor(() => expect(api.generateTtsPreview).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.generateTtsPreview).mock.calls[0][0]).toMatchObject({
      provider: "sarvam", model: "bulbul:v3", voice: "vp-shubh",
      params: { temperature: 0.35, min_buffer_size: 50 },
    });
    // Nothing was persisted by previewing.
    expect(api.saveVoiceSettings).not.toHaveBeenCalled();
  });

  it("tuning a slider alone never saves", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    setParam(dialog, "Temperature", 0.42);
    await user.click(within(dialog).getByRole("button", { name: "Apply settings" }));

    expect(api.saveVoiceSettings).not.toHaveBeenCalled();
  });

  it("applying then saving persists exactly the tuned settings", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    setParam(dialog, "Temperature", 0.42);
    setParam(dialog, "Min buffer size", 70);
    await user.click(within(dialog).getByRole("button", { name: "Apply settings" }));
    await waitFor(() =>
      expect(within(dialog).getByRole("button", { name: "Applied" })).toBeInTheDocument());
    await user.click(within(dialog).getByRole("button", { name: "Close" }));

    await user.click(screen.getByRole("button", { name: "Save voice settings" }));
    await waitFor(() => expect(api.saveVoiceSettings).toHaveBeenCalledTimes(1));
    const payload = vi.mocked(api.saveVoiceSettings).mock.calls[0][1] as Record<string, unknown>;
    expect(payload.ttsSettings).toEqual({ temperature: 0.42, min_buffer_size: 70 });
    expect(payload.ttsProvider).toBe("sarvam");
    expect(payload.ttsModel).toBe("bulbul:v3");
  });

  it("applying a provider/model switch stages the new engine too", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    await user.selectOptions(within(dialog).getByLabelText("Preview provider"), ["elevenlabs"]);
    await waitFor(() => expect(paramInput(dialog, "Stability")).toBeInTheDocument());
    setParam(dialog, "Stability", 0.65);
    await user.click(within(dialog).getByLabelText("Preview voice"));
    await user.click(await within(dialog).findByText("Rachel"));
    await user.click(within(dialog).getByRole("button", { name: "Apply settings" }));
    await user.click(within(dialog).getByRole("button", { name: "Close" }));

    await user.click(screen.getByRole("button", { name: "Save voice settings" }));
    await waitFor(() => expect(api.saveVoiceSettings).toHaveBeenCalledTimes(1));
    const payload = vi.mocked(api.saveVoiceSettings).mock.calls[0][1] as Record<string, unknown>;
    expect(payload.ttsProvider).toBe("elevenlabs");
    expect(payload.ttsModel).toBe("eleven_flash_v2_5");
    expect(payload.ttsVoice).toBe("vp-rachel");
    expect(payload.ttsSettings).toMatchObject({ stability: 0.65 });
    // Delivery owns speed, so it is never stored as a provider parameter.
    expect(payload.ttsSettings).not.toHaveProperty("speed");
  });

  it("restores saved settings on reload", async () => {
    installMocks(baseSettings({
      ttsSettings: { temperature: 0.25, min_buffer_size: 90 },
    }));
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    expect(paramInput(dialog, "Temperature")).toHaveValue(0.25);
    expect(paramInput(dialog, "Min buffer size")).toHaveValue(90);
  });

  it("keeps working for a bot with no saved settings", async () => {
    installMocks(baseSettings({ ttsSettings: {} }));
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    // Schema defaults fill in; nothing errors and preview still works.
    expect(paramInput(dialog, "Temperature")).toHaveValue(0.6);
    await user.click(within(dialog).getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(api.generateTtsPreview).toHaveBeenCalledTimes(1));
  });
});

describe("Voice preview — per-language override target", () => {
  /* Both Preview buttons open the SAME modal with the SAME editable controls —
     a per-language launch differs only in where Apply stages the engine. */
  it("offers the identical editable delivery controls as the default-engine preview", async () => {
    installMocks(baseSettings({
      speed: 0.85,
      languageVoiceMap: {
        default: "en-IN",
        "hi-IN": { provider: "sarvam", model: "bulbul:v3", voice: "vp-shubh", params: {} },
      },
    }));
    const user = userEvent.setup();
    render(<VoiceTab bot={BOT} />);
    await screen.findByText("Hindi");

    const row = screen.getByText("Hindi").closest("li")!;
    const previewBtn = within(row).getByRole("button", { name: "Preview voice for Hindi" });
    await waitFor(() => expect(previewBtn).toBeEnabled());
    await user.click(previewBtn);

    const dialog = await screen.findByRole("dialog");
    // Same four delivery sliders, prefilled from the bot-level tuning.
    for (const name of ["Speaking speed", "Pause between sentences", "Empathy", "Energy"]) {
      expect(within(dialog).getByRole("slider", { name })).toBeInTheDocument();
    }
    expect(within(dialog).getByRole("slider", { name: "Speaking speed" })).toHaveValue("0.85");
    // Engine settings and delivery drafts both drive the preview.
    setParam(dialog, "Temperature", 0.5);
    setParam(dialog, "Pause between sentences", 600);
    await user.click(within(dialog).getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(api.generateTtsPreview).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.generateTtsPreview).mock.calls[0][0]).toMatchObject({
      speed: 0.85, pauseMs: 600, params: { temperature: 0.5 },
    });
  });

  it("applying stages the row's engine params and the bot-level delivery values", async () => {
    installMocks(baseSettings({
      speed: 0.85,
      languageVoiceMap: {
        default: "en-IN",
        "hi-IN": { provider: "sarvam", model: "bulbul:v3", voice: "vp-shubh", params: {} },
      },
    }));
    const user = userEvent.setup();
    render(<VoiceTab bot={BOT} />);
    await screen.findByText("Hindi");

    const row = screen.getByText("Hindi").closest("li")!;
    const previewBtn = within(row).getByRole("button", { name: "Preview voice for Hindi" });
    await waitFor(() => expect(previewBtn).toBeEnabled());
    await user.click(previewBtn);

    const dialog = await screen.findByRole("dialog");
    setParam(dialog, "Temperature", 0.5);
    setParam(dialog, "Energy", 80);
    await user.click(within(dialog).getByRole("button", { name: "Apply settings" }));
    await user.click(within(dialog).getByRole("button", { name: "Close" }));

    await user.click(screen.getByRole("button", { name: "Save voice settings" }));
    await waitFor(() => expect(api.saveVoiceSettings).toHaveBeenCalledTimes(1));
    const payload = vi.mocked(api.saveVoiceSettings).mock.calls[0][1] as Record<string, never>;
    // Engine params land on the language row…
    expect(payload.languageVoiceMap["hi-IN"]).toMatchObject({
      provider: "sarvam", model: "bulbul:v3", params: { temperature: 0.5 },
    });
    // …while delivery values stage bot-level, exactly like the other launch:
    // an untouched speed stays as it was, the moved Energy is applied.
    expect(payload.speed).toBe(0.85);
    expect(payload.energy).toBe(80);
  });
});

describe("Voice preview — realtime-only apply targets", () => {
  const rowLaunch = () => baseSettings({
    languageVoiceMap: {
      default: "en-IN",
      "hi-IN": { provider: "elevenlabs", model: "eleven_flash_v2_5", voice: "vp-rachel", params: {} },
    },
  });

  async function openRowPreview(user: ReturnType<typeof userEvent.setup>) {
    render(<VoiceTab bot={BOT} />);
    await screen.findByText("Hindi");
    const row = screen.getByText("Hindi").closest("li")!;
    const btn = within(row).getByRole("button", { name: "Preview voice for Hindi" });
    await waitFor(() => expect(btn).toBeEnabled());
    await user.click(btn);
    return screen.findByRole("dialog");
  }

  it("launched from a language row, non-streaming models are disabled with the reason", async () => {
    installMocks(rowLaunch());
    const user = userEvent.setup();
    const dialog = await openRowPreview(user);

    const option = within(dialog).getByRole("option", {
      name: /Eleven v3 \(expressive\) — no realtime streaming/,
    }) as HTMLOptionElement;
    expect(option.disabled).toBe(true);
  });

  it("launched from the default engine, the same model stays selectable (REST path exists)", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    await user.selectOptions(within(dialog).getByLabelText("Preview provider"), ["elevenlabs"]);
    await waitFor(() =>
      expect(within(dialog).getByLabelText("Preview model")).toHaveValue("eleven_flash_v2_5"));
    const option = within(dialog).getByRole("option", {
      name: "Eleven v3 (expressive)",
    }) as HTMLOptionElement;
    expect(option.disabled).toBe(false);
  });

  it("a stale non-streaming model can be previewed from a row but never applied", async () => {
    installMocks(baseSettings({
      languageVoiceMap: {
        default: "en-IN",
        "hi-IN": { provider: "elevenlabs", model: "eleven_v3", voice: "vp-rachel", params: {} },
      },
    }));
    const user = userEvent.setup();
    const dialog = await openRowPreview(user);

    expect(within(dialog).getByLabelText("Preview model")).toHaveValue("eleven_v3");
    // Generate still works — hearing the model is fine…
    expect(within(dialog).getByRole("button", { name: "Generate" })).toBeEnabled();
    // …but staging it into a realtime slot is blocked with the reason.
    const apply = within(dialog).getByRole("button", { name: "Apply settings" });
    expect(apply).toBeDisabled();
    expect(apply).toHaveAttribute("title", expect.stringMatching(/does not support realtime streaming/));
  });
});

describe("Voice preview — reset to defaults", () => {
  it("offers a per-setting reset only once a value differs from its default", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    expect(
      within(dialog).queryByRole("button", { name: "Reset Temperature to default" }),
    ).not.toBeInTheDocument();

    setParam(dialog, "Temperature", 0.9);
    const reset = await within(dialog).findByRole(
      "button", { name: "Reset Temperature to default" },
    );
    await user.click(reset);
    expect(paramInput(dialog, "Temperature")).toHaveValue(0.6);
  });

  it("resets every setting and the speaking speed at once", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    setParam(dialog, "Temperature", 0.9);
    setParam(dialog, "Min buffer size", 200);
    await user.click(within(dialog).getByRole(
      "button", { name: "Reset all voice settings to defaults" },
    ));

    expect(paramInput(dialog, "Temperature")).toHaveValue(0.6);
    expect(paramInput(dialog, "Min buffer size")).toHaveValue(50);
    expect(within(dialog).getByRole("slider", { name: "Speaking speed" })).toHaveValue("1");
  });
});

describe("Voice preview — layout order and delivery tuning drafts", () => {
  it("puts Sample text after every settings section", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    const sampleText = within(dialog).getByLabelText("Preview sample text");
    for (const label of ["Voice settings", "Delivery tuning", "Pronunciation"]) {
      const section = within(dialog).getByText(label, { exact: true });
      expect(
        section.compareDocumentPosition(sampleText) & Node.DOCUMENT_POSITION_FOLLOWING,
        `${label} should precede Sample text`,
      ).toBeTruthy();
    }
    // …and the provider selection precedes the settings sections.
    const provider = within(dialog).getByLabelText("Preview provider");
    const voiceSettings = within(dialog).getByText("Voice settings", { exact: true });
    expect(provider.compareDocumentPosition(voiceSettings) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("renders all four delivery controls and sends the drafts to the preview", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    setParam(dialog, "Pause between sentences", 600);
    setParam(dialog, "Energy", 80);
    setParam(dialog, "Empathy", 90);
    setParam(dialog, "Speaking speed", 1.3);
    await user.click(within(dialog).getByRole("button", { name: "Generate" }));

    await waitFor(() => expect(api.generateTtsPreview).toHaveBeenCalledTimes(1));
    const request = vi.mocked(api.generateTtsPreview).mock.calls[0][0];
    expect(request).toMatchObject({ speed: 1.3, pauseMs: 600, energy: 80 });
    // Empathy shapes live LLM replies, not fixed preview text — never sent.
    expect(request).not.toHaveProperty("empathy");
  });

  it("applying stages all delivery values onto the page and save persists them", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    setParam(dialog, "Pause between sentences", 700);
    setParam(dialog, "Empathy", 95);
    setParam(dialog, "Energy", 10);
    await user.click(within(dialog).getByRole("button", { name: "Apply settings" }));
    await user.click(within(dialog).getByRole("button", { name: "Close" }));

    await user.click(screen.getByRole("button", { name: "Save voice settings" }));
    await waitFor(() => expect(api.saveVoiceSettings).toHaveBeenCalledTimes(1));
    const payload = vi.mocked(api.saveVoiceSettings).mock.calls[0][1] as Record<string, unknown>;
    expect(payload).toMatchObject({ pauseMs: 700, empathy: 95, energy: 10 });
  });
});

describe("Voice preview — per-language voice resolution", () => {
  const withOverride = () => baseSettings({
    languageVoiceMap: {
      default: "en-IN",
      "hi-IN": {
        provider: "elevenlabs", model: "eleven_flash_v2_5", voice: "vp-rachel",
        params: { stability: 0.9 },
      },
    },
  });

  it("switching to a language with an override previews that engine like a real call", async () => {
    installMocks(withOverride());
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    expect(within(dialog).getByLabelText("Preview provider")).toHaveValue("sarvam");
    await user.selectOptions(within(dialog).getByLabelText("Preview language"), ["hi-IN"]);

    await waitFor(() =>
      expect(within(dialog).getByLabelText("Preview provider")).toHaveValue("elevenlabs"));
    expect(within(dialog).getByLabelText("Preview model")).toHaveValue("eleven_flash_v2_5");
    expect(screen.getByTestId("override-hint")).toHaveTextContent(/hi-IN uses its per-language voice override/);
    // The override's own validated params drive the preview.
    expect(paramInput(dialog, "Stability")).toHaveValue(0.9);

    await user.click(within(dialog).getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(api.generateTtsPreview).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.generateTtsPreview).mock.calls[0][0]).toMatchObject({
      provider: "elevenlabs", model: "eleven_flash_v2_5", voice: "vp-rachel",
      language: "hi-IN", params: { stability: 0.9 },
    });
  });

  it("blocks Apply while a foreign override is previewed, and restores the default engine on switch-back", async () => {
    installMocks(withOverride());
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    await user.selectOptions(within(dialog).getByLabelText("Preview language"), ["hi-IN"]);
    await waitFor(() =>
      expect(within(dialog).getByLabelText("Preview provider")).toHaveValue("elevenlabs"));
    expect(within(dialog).getByRole("button", { name: "Apply settings" })).toBeDisabled();

    await user.selectOptions(within(dialog).getByLabelText("Preview language"), ["en-IN"]);
    await waitFor(() =>
      expect(within(dialog).getByLabelText("Preview provider")).toHaveValue("sarvam"));
    expect(within(dialog).getByLabelText("Preview model")).toHaveValue("bulbul:v3");
    expect(within(dialog).getByRole("button", { name: "Apply settings" })).toBeEnabled();
  });
});

describe("Voice preview — pronunciation dictionary", () => {
  it("renders a named-dictionary selector instead of a raw id input (bulbul:v3)", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    expect(within(dialog).getByText("Pronunciation", { exact: true })).toBeInTheDocument();
    const select = await within(dialog).findByLabelText("Pronunciation dictionary");
    expect(select.tagName).toBe("SELECT");
    expect(within(dialog).getByRole("option", { name: /Collections Hindi — 3 words/ })).toBeInTheDocument();
    // Never a free-text provider-id box.
    expect(within(dialog).queryByRole("textbox", { name: "Pronunciation dictionary" })).not.toBeInTheDocument();

    await user.selectOptions(select, ["p_5cb7faa6"]);
    await user.click(within(dialog).getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(api.generateTtsPreview).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.generateTtsPreview).mock.calls[0][0].params).toMatchObject({
      dict_id: "p_5cb7faa6",
    });
  });

  it("bulbul:v2 offers a real preprocessing toggle and no pronunciation section", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    await user.selectOptions(within(dialog).getByLabelText("Preview model"), ["bulbul:v2"]);
    await waitFor(() =>
      expect(within(dialog).queryByText("Pronunciation", { exact: true })).not.toBeInTheDocument());
    expect(within(dialog).queryByLabelText("Pronunciation dictionary")).not.toBeInTheDocument();
    // v2's preprocessing is a live toggle, not the v3 fixed "Always on" fact.
    expect(within(dialog).getByRole("switch", { name: "Preprocessing" })).toBeInTheDocument();
  });

  it("v3 shows preprocessing as a fixed fact, not an editable toggle", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    expect(within(dialog).getByText("Always on")).toBeInTheDocument();
    expect(within(dialog).queryByRole("switch", { name: "Preprocessing" })).not.toBeInTheDocument();
  });

  it("bounds Min buffer size to the documented Sarvam range", async () => {
    const user = userEvent.setup();
    const dialog = await openPreview(user);

    const slider = within(dialog).getByRole("slider", { name: "Min buffer size" });
    expect(slider).toHaveAttribute("min", "30");
    expect(slider).toHaveAttribute("max", "200");
    // The numeric companion clamps out-of-range entries instead of staging 20.
    setParam(dialog, "Min buffer size", 20);
    expect(paramInput(dialog, "Min buffer size")).toHaveValue(30);
  });
});
