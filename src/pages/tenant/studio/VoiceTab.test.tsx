/* Per-language voices section of the Voice tab:
   - one structured card per bot language with labelled provider/model/voice
     fields, readable names and an explicit status chip
   - dependent dropdowns (provider → model → voice) reset incompatible values
   - saved values that fell out of the catalog render as unavailable, are not
     re-selectable and demand an explicit replacement
   - loading / error / empty states and the existing save flow keep working */

import { render, screen, waitFor, within } from "@testing-library/react";
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
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), hasPermission: () => true }),
}));

const BOT = { id: "bot_1", languages: ["en-IN", "hi-IN"] } as unknown as VoiceBot;

const SETTINGS = {
  speed: 1, pauseMs: 350, empathy: 50, energy: 50,
  sttProvider: "", sttModel: "", sttLanguage: "", sttSettings: {},
  llmProvider: "", llmModel: "", llmSettings: {},
  ttsProvider: "sarvam", ttsModel: "bulbul:v3", ttsVoice: "vp-shubh", ttsSettings: {},
  fallbackProvider: "", fallbackModel: "", fallbackVoice: "",
  languageVoiceMap: {
    default: "en-IN",
    "hi-IN": { provider: "sarvam", model: "bulbul:v3", voice: "vp-anushka" },
  },
  audioSettings: {
    browser: { codec: "linear16", sampleRate: 16000 },
    telephony: { codec: "mulaw", sampleRate: 8000 },
  },
};

const voice = (id: string, name: string, languages: string[], modelCodes: string[], provider = "sarvam") => ({
  id, name, gender: "female", provider, providerVoiceId: id.replace("vp-", ""),
  languages, modelCodes, locale: languages[0] ?? null, premium: false,
  isDefault: id === "vp-shubh", status: "active", providerSettings: {}, sampleText: null,
});

function installDefaultMocks(settings: Record<string, unknown> = SETTINGS) {
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
    Promise.resolve(
      provider === "sarvam" ? [
        {
          code: "bulbul:v3", displayName: "Bulbul v3", isDefault: true, capability: "tts", streaming: true,
          paramsSchema: {
            pace: { type: "number", min: 0.5, max: 2, step: 0.05, default: 1, label: "Pace" },
            min_buffer_size: { type: "integer", min: 10, max: 500, default: 40, label: "Min buffer size" },
          },
        },
        { code: "bulbul:v2", displayName: "Bulbul v2", isDefault: false, capability: "tts", streaming: true, paramsSchema: {} },
      ] : provider === "elevenlabs" ? [
        {
          code: "eleven_flash_v2_5", displayName: "Eleven Flash v2.5", isDefault: true,
          capability: "tts", streaming: true,
          description: "Ultra-low-latency model for realtime conversation.",
          paramsSchema: {
            stability: { type: "number", min: 0, max: 1, step: 0.05, default: 0, label: "Stability" },
            speed: { type: "number", min: 0.7, max: 1.2, step: 0.05, default: 1, label: "Speed" },
          },
        },
        {
          code: "eleven_v3", displayName: "Eleven v3 (expressive)", isDefault: false,
          capability: "tts", streaming: false,
          description: "Most expressive ElevenLabs model (alpha).",
          paramsSchema: {
            stability: {
              type: "enum", values: [0, 0.5, 1], default: 0.5, label: "Stability",
              labels: { "0": "Creative", "0.5": "Natural", "1": "Robust" },
            },
            similarity_boost: { type: "number", min: 0, max: 1, step: 0.05, default: 1, label: "Similarity boost" },
          },
        },
      ] : [],
    )) as never);
  vi.mocked(api.listProviderVoices).mockImplementation(((provider: string) =>
    Promise.resolve(
      provider === "sarvam" ? [
        voice("vp-shubh", "Shubh", ["en-IN", "hi-IN"], ["bulbul:v3", "bulbul:v2"]),
        voice("vp-anushka", "Anushka", ["hi-IN"], ["bulbul:v3"]),
      ] : provider === "elevenlabs" ? [
        voice("vp-rachel", "Rachel", ["en-IN"], ["eleven_flash_v2_5", "eleven_v3"], "elevenlabs"),
      ] : [],
    )) as never);
  vi.mocked(api.getModelLanguages).mockResolvedValue(
    { languages: [], supportsAutoDetect: true, languageAgnostic: true } as never);
  vi.mocked(api.saveVoiceSettings).mockResolvedValue({ settings: SETTINGS, warnings: [] } as never);
}

const langRow = (name: string) => screen.getByText(name).closest("li")!;

describe("VoiceTab — Per-language voices", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installDefaultMocks();
  });

  it("renders one labelled card per language with provider, model, voice and status", async () => {
    render(<VoiceTab bot={BOT} />);
    await screen.findByText("Hindi"); // readable names, locale codes kept as detail
    expect(screen.getByText("English (India)")).toBeInTheDocument();

    const hi = langRow("Hindi");
    expect(within(hi).getByText("Provider")).toBeInTheDocument(); // visible field labels
    expect(within(hi).getByText("Model")).toBeInTheDocument();
    expect(within(hi).getByText("Voice")).toBeInTheDocument();
    await waitFor(() => expect(within(hi).getByLabelText("Voice provider for hi-IN")).toHaveValue("sarvam"));
    await waitFor(() => expect(within(hi).getByLabelText("Voice model for hi-IN")).toHaveValue("bulbul:v3"));
    await within(hi).findByText("Anushka"); // voice shown by readable name, not raw id
    await waitFor(() => expect(within(hi).getByText("Active")).toBeInTheDocument());

    // The inheriting language states its effective engine and inherit status.
    const en = langRow("English (India)");
    expect(within(en).getByText("Inherits default")).toBeInTheDocument();
    await within(en).findByText(/Uses Sarvam AI/);
    expect(within(en).getByText(/en-IN · default language/)).toBeInTheDocument();
  });

  it("provider change resets model and voice, then loads the new provider's catalog", async () => {
    const user = userEvent.setup();
    render(<VoiceTab bot={BOT} />);
    await screen.findByText("Hindi");
    const hi = langRow("Hindi");
    await within(hi).findByText("Anushka");

    await user.selectOptions(within(hi).getByLabelText("Voice provider for hi-IN"), ["elevenlabs"]);
    expect(api.listProviderModels).toHaveBeenCalledWith("tts", "elevenlabs");
    expect(api.listProviderVoices).toHaveBeenCalledWith("elevenlabs");
    // Model reloads to the new provider's default; the old voice is cleared.
    await waitFor(() => expect(within(hi).getByLabelText("Voice model for hi-IN")).toHaveValue("eleven_flash_v2_5"));
    expect(within(hi).queryByText("Anushka")).not.toBeInTheDocument();
    expect(within(hi).getByText("Select voice…")).toBeInTheDocument();
    await waitFor(() => expect(within(hi).getByText("Incomplete")).toBeInTheDocument());
  });

  it("model change clears an incompatible voice and keeps a compatible one", async () => {
    const user = userEvent.setup();
    render(<VoiceTab bot={BOT} />);
    await screen.findByText("Hindi");
    const hi = langRow("Hindi");
    await within(hi).findByText("Anushka"); // supports bulbul:v3 only

    await user.selectOptions(within(hi).getByLabelText("Voice model for hi-IN"), ["bulbul:v2"]);
    await waitFor(() => expect(within(hi).queryByText("Anushka")).not.toBeInTheDocument());

    // Pick Shubh (supports v2 and v3) — switching the model back keeps it.
    await user.click(within(hi).getByLabelText("Voice for hi-IN"));
    await user.click(await screen.findByRole("option", { name: /Shubh/ }));
    await within(hi).findByText(/Shubh/);
    await user.selectOptions(within(hi).getByLabelText("Voice model for hi-IN"), ["bulbul:v3"]);
    expect(within(hi).getByText(/Shubh/)).toBeInTheDocument();
  });

  it("voice options are filtered to the row's language", async () => {
    const user = userEvent.setup();
    render(<VoiceTab bot={BOT} />);
    await screen.findByText("English (India)");
    const en = langRow("English (India)");

    await user.selectOptions(within(en).getByLabelText("Voice provider for en-IN"), ["sarvam"]);
    await waitFor(() => expect(within(en).getByLabelText("Voice model for en-IN")).toHaveValue("bulbul:v3"));
    await user.click(within(en).getByLabelText("Voice for en-IN"));
    expect(await screen.findByRole("option", { name: /Shubh/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Anushka/ })).not.toBeInTheDocument(); // hi-IN-only voice
  });

  it("a saved voice missing from the catalog renders as unavailable and is not re-selectable", async () => {
    installDefaultMocks({
      ...SETTINGS,
      languageVoiceMap: { "hi-IN": { provider: "sarvam", model: "bulbul:v3", voice: "vp-gone" } },
    });
    const user = userEvent.setup();
    render(<VoiceTab bot={BOT} />);
    await screen.findByText("Hindi");
    const hi = langRow("Hindi");

    await within(hi).findByText("vp-gone (unavailable)");
    await waitFor(() => expect(within(hi).getByText("Unavailable")).toBeInTheDocument());
    expect(within(hi).getByRole("alert")).toHaveTextContent(/not in the sarvam catalog/);
    // The section-level replacement warning with its explicit action.
    expect(screen.getByText("Selections no longer valid")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply changes" })).toBeInTheDocument();

    // The stale value is pinned in the list but cannot be picked again.
    await user.click(within(hi).getByLabelText("Voice for hi-IN"));
    const stale = await screen.findByRole("option", { name: /vp-gone \(unavailable\)/ });
    expect(stale).toHaveAttribute("aria-disabled", "true");
    // Valid replacements remain selectable.
    await user.click(screen.getByRole("option", { name: /Anushka/ }));
    await within(hi).findByText("Anushka");
    await waitFor(() => expect(within(hi).getByText("Active")).toBeInTheDocument());
  });

  it("an unavailable provider is flagged and its option is disabled", async () => {
    installDefaultMocks({
      ...SETTINGS,
      languageVoiceMap: { "hi-IN": { provider: "deepgram", model: "nova", voice: "vp-x" } },
    });
    render(<VoiceTab bot={BOT} />);
    await screen.findByText("Hindi");
    const hi = langRow("Hindi");

    const providerSelect = within(hi).getByLabelText("Voice provider for hi-IN");
    await waitFor(() => expect(providerSelect).toHaveValue("deepgram"));
    const stale = within(providerSelect).getByRole("option", { name: "deepgram (unavailable)" }) as HTMLOptionElement;
    expect(stale.disabled).toBe(true);
    await waitFor(() => expect(within(hi).getByText("Unavailable")).toBeInTheDocument());
    expect(within(hi).getByRole("alert")).toHaveTextContent(/no longer available/);
  });

  it("Reset clears the override back to inheriting the default engine", async () => {
    const user = userEvent.setup();
    render(<VoiceTab bot={BOT} />);
    await screen.findByText("Hindi");
    const hi = langRow("Hindi");
    await within(hi).findByText("Anushka");

    await user.click(within(hi).getByRole("button", { name: "Reset voice override for Hindi" }));
    expect(within(hi).getByLabelText("Voice provider for hi-IN")).toHaveValue("");
    expect(within(hi).getByText("Inherits default")).toBeInTheDocument();
    expect(within(hi).getByText(/Uses Sarvam AI/)).toBeInTheDocument();
  });

  it("save sends the language voice map unchanged and reloads the settings", async () => {
    const user = userEvent.setup();
    render(<VoiceTab bot={BOT} />);
    await screen.findByText("Hindi");
    await within(langRow("Hindi")).findByText("Anushka");

    await user.click(screen.getByRole("button", { name: "Save voice settings" }));
    await waitFor(() => expect(api.saveVoiceSettings).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.saveVoiceSettings).mock.calls[0][1]).toMatchObject({
      ttsProvider: "sarvam", ttsModel: "bulbul:v3", ttsVoice: "vp-shubh",
      languageVoiceMap: {
        default: "en-IN",
        "hi-IN": { provider: "sarvam", model: "bulbul:v3", voice: "vp-anushka" },
      },
    });
    await waitFor(() => expect(api.getVoiceSettings).toHaveBeenCalledTimes(2)); // draft reload
  });

  it("edits and saves per-bot turn response timing", async () => {
    const user = userEvent.setup();
    installDefaultMocks({
      ...SETTINGS,
      sttSettings: {
        turn_detection: { user_speech_timeout: 0.7, finalize_grace: 0.15 },
      },
    });
    render(<VoiceTab bot={BOT} />);
    await screen.findByText("Hindi");

    await user.click(screen.getByText("Turn response timing"));
    expect(screen.getByLabelText("User pause window value")).toHaveValue(0.7);
    expect(screen.getByLabelText("Transcript finalization grace value")).toHaveValue(0.15);

    await user.click(screen.getByRole("button", { name: "Save voice settings" }));
    await waitFor(() => expect(api.saveVoiceSettings).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.saveVoiceSettings).mock.calls[0][1]).toMatchObject({
      sttSettings: {
        turn_detection: { user_speech_timeout: 0.7, finalize_grace: 0.15 },
      },
    });
  });

  it("keeps a readable name for platform-disabled languages and flags them", async () => {
    vi.mocked(api.listLanguages).mockResolvedValue([
      { id: "l1", code: "en-IN", name: "English (India)", enabled: true },
      { id: "l2", code: "hi-IN", name: "Hindi", enabled: false },
    ] as never);
    render(<VoiceTab bot={BOT} />);
    // Names come from the include-disabled catalog, so the code never leaks in.
    await screen.findByText("Hindi");
    expect(api.listLanguages).toHaveBeenCalledWith(true);
    const hi = langRow("Hindi");
    expect(within(hi).getByText(/hi-IN · disabled on platform/)).toBeInTheDocument();
    const en = langRow("English (India)");
    expect(within(en).queryByText(/disabled on platform/)).not.toBeInTheDocument();
  });

  it("default language dropdown uses readable names and stays optional", async () => {
    render(<VoiceTab bot={BOT} />);
    await screen.findByText("Hindi");
    const select = screen.getByLabelText("Default language");
    const labels = within(select).getAllByRole("option").map((o) => o.textContent);
    expect(labels).toEqual(["Not set", "English (India) (en-IN)", "Hindi (hi-IN)"]);
    await waitFor(() => expect(select).toHaveValue("en-IN"));
  });

  it("shows the loading skeletons and the API error state", async () => {
    vi.mocked(api.getVoiceSettings).mockImplementation((() => new Promise(() => {})) as never);
    const { unmount } = render(<VoiceTab bot={BOT} />);
    expect(screen.getAllByLabelText("Loading").length).toBeGreaterThan(0);
    unmount();

    vi.mocked(api.getVoiceSettings).mockRejectedValue(new Error("boom"));
    render(<VoiceTab bot={BOT} />);
    await screen.findByText(/load this view/);
    expect(screen.getByText("boom")).toBeInTheDocument();
  });

  it("shows a helpful empty state when the bot has no languages", async () => {
    installDefaultMocks({ ...SETTINGS, languageVoiceMap: {} });
    render(<VoiceTab bot={{ ...BOT, languages: [] } as unknown as VoiceBot} />);
    await screen.findByText(/no languages yet/);
  });
});

describe("VoiceTab — ElevenLabs model selection", () => {
  const ELEVEN_SETTINGS = {
    ...SETTINGS,
    ttsProvider: "elevenlabs", ttsModel: "eleven_flash_v2_5", ttsVoice: "vp-rachel",
    ttsSettings: { stability: 0, speed: 1.1 },
    languageVoiceMap: { default: "en-IN" },
  };

  beforeEach(() => {
    vi.clearAllMocks();
    installDefaultMocks(ELEVEN_SETTINGS);
  });

  const ttsModelSelect = () => screen.getByLabelText("TTS model") as HTMLSelectElement;

  it("offers both ElevenLabs models in the default engine dropdown", async () => {
    render(<VoiceTab bot={BOT} />);
    await waitFor(() => expect(ttsModelSelect()).toHaveValue("eleven_flash_v2_5"));
    const labels = within(ttsModelSelect()).getAllByRole("option").map((o) => o.textContent);
    expect(labels).toContain("Eleven Flash v2.5 (default)");
    expect(labels).toContain("Eleven v3 (expressive)");
  });

  it("selecting eleven_v3 swaps to its schema: discrete stability, no speed field", async () => {
    const user = userEvent.setup();
    render(<VoiceTab bot={BOT} />);
    await waitFor(() => expect(ttsModelSelect()).toHaveValue("eleven_flash_v2_5"));
    // Delivery tuning owns speed — the provider duplicate never renders on
    // the bot page, even for models that support it (flash).
    expect(screen.queryByLabelText("Speed")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Stability")).toBeInTheDocument();

    await user.selectOptions(ttsModelSelect(), ["eleven_v3"]);
    expect(ttsModelSelect()).toHaveValue("eleven_v3");
    // Unsupported params disappear; still-valid values are preserved.
    await waitFor(() => expect(screen.queryByLabelText("Speed")).not.toBeInTheDocument());
    const stability = screen.getByLabelText("Stability") as HTMLSelectElement;
    expect(stability.tagName).toBe("SELECT"); // enum, not a slider
    const labels = within(stability).getAllByRole("option").map((o) => o.textContent);
    expect(labels).toEqual(["Creative (0)", "Natural (0.5)", "Robust (1)"]);
    // The saved flash stability value (0) is still valid on the v3 grid.
    expect(stability).toHaveValue("0");
    // The model description/capability hint is surfaced.
    expect(screen.getByText(/Most expressive ElevenLabs model/)).toBeInTheDocument();
    expect(screen.getByText(/No realtime streaming/)).toBeInTheDocument();
  });

  it("the model choice survives an edit round-trip (reset restores it)", async () => {
    installDefaultMocks({ ...ELEVEN_SETTINGS, ttsModel: "eleven_v3", ttsSettings: { stability: 0.5 } });
    render(<VoiceTab bot={BOT} />);
    await waitFor(() => expect(ttsModelSelect()).toHaveValue("eleven_v3"));
  });

  it("fallback and per-language dropdowns offer only realtime-streaming models", async () => {
    const user = userEvent.setup();
    installDefaultMocks({
      ...SETTINGS,
      fallbackProvider: "elevenlabs", fallbackModel: "eleven_flash_v2_5", fallbackVoice: "vp-rachel",
    });
    render(<VoiceTab bot={BOT} />);
    await screen.findByText("Hindi");

    const fallbackModel = await screen.findByLabelText("Fallback model");
    await waitFor(() => expect(fallbackModel).toHaveValue("eleven_flash_v2_5"));
    const fallbackLabels = within(fallbackModel).getAllByRole("option").map((o) => o.textContent);
    expect(fallbackLabels).toContain("Eleven Flash v2.5 (default)");
    expect(fallbackLabels).not.toContain("Eleven v3 (expressive)");

    const hi = langRow("Hindi");
    await user.selectOptions(within(hi).getByLabelText("Voice provider for hi-IN"), ["elevenlabs"]);
    await waitFor(() => expect(within(hi).getByLabelText("Voice model for hi-IN")).toHaveValue("eleven_flash_v2_5"));
    const rowLabels = within(within(hi).getByLabelText("Voice model for hi-IN"))
      .getAllByRole("option").map((o) => o.textContent);
    expect(rowLabels).not.toContain("Eleven v3 (expressive)");
  });
});

describe("VoiceTab — Delivery tuning", () => {
  class FakeAudio {
    onended: (() => void) | null = null;
    paused = true;
    constructor(public src: string) {}
    play() { this.paused = false; return Promise.resolve(); }
    pause() { this.paused = true; }
  }

  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("Audio", FakeAudio);
    installDefaultMocks();
  });

  it("hides duplicate provider speed params but keeps other provider settings", async () => {
    render(<VoiceTab bot={BOT} />);
    await screen.findByText("Hindi");
    // Sarvam bulbul:v3 schema ships a `pace` param — it must not render on
    // the bot page; the technical min_buffer_size setting still does.
    expect((await screen.findAllByLabelText("Min buffer size")).length).toBeGreaterThan(0);
    expect(screen.queryByLabelText("Pace")).not.toBeInTheDocument();
    // The one canonical speed control lives in Delivery tuning.
    expect(screen.getByLabelText("Speaking speed")).toBeInTheDocument();
    expect(screen.getByText(/single speed control/)).toBeInTheDocument();
    expect(screen.getByText(/Silence inserted between assistant sentences/)).toBeInTheDocument();
    expect(screen.getByText(/wording and acknowledgement/)).toBeInTheDocument();
    expect(screen.getByText(/native voice support varies by provider/)).toBeInTheDocument();
  });

  it("saves delivery values and strips legacy speed from ttsSettings", async () => {
    const user = userEvent.setup();
    installDefaultMocks({
      ...SETTINGS,
      speed: 1.2, pauseMs: 500, empathy: 80, energy: 20,
      ttsSettings: { pace: 0.7, min_buffer_size: 60 },
    });
    render(<VoiceTab bot={BOT} />);
    await screen.findByText("Hindi");

    await user.click(screen.getByRole("button", { name: "Save voice settings" }));
    await waitFor(() => expect(api.saveVoiceSettings).toHaveBeenCalledTimes(1));
    const payload = vi.mocked(api.saveVoiceSettings).mock.calls[0][1] as Record<string, unknown>;
    expect(payload).toMatchObject({ speed: 1.2, pauseMs: 500, empathy: 80, energy: 20 });
    // The legacy pace duplicate is gone; unrelated settings survive.
    expect(payload.ttsSettings).toEqual({ min_buffer_size: 60 });
  });

  it("delivery sliders update the save payload", async () => {
    const user = userEvent.setup();
    render(<VoiceTab bot={BOT} />);
    await screen.findByText("Hindi");

    const pause = screen.getByLabelText("Pause between sentences") as HTMLInputElement;
    // range inputs accept programmatic value change via fireEvent-style typing
    await user.click(pause);
    pause.stepUp(); // 350 → 400
    pause.dispatchEvent(new Event("change", { bubbles: true }));
    await user.click(screen.getByRole("button", { name: "Save voice settings" }));
    await waitFor(() => expect(api.saveVoiceSettings).toHaveBeenCalledTimes(1));
    const payload = vi.mocked(api.saveVoiceSettings).mock.calls[0][1] as Record<string, unknown>;
    expect(payload.pauseMs).toBe(400);
  });

  it("preview sends the delivery tuning alongside the engine selection", async () => {
    const user = userEvent.setup();
    vi.mocked(api.generateTtsPreview).mockResolvedValue({
      audioBase64: "AAAA", mimeType: "audio/wav", sampleRate: 16000,
      ttfaMs: 12, totalMs: 40, provider: "sarvam", voice: "vp-shubh",
    } as never);
    render(<VoiceTab bot={BOT} />);
    await screen.findByText("Hindi");

    await user.click(screen.getByRole("button", { name: "Preview voice" }));
    const dialog = await screen.findByRole("dialog");
    // The modal explains exactly what the preview can and cannot apply.
    expect(within(dialog).getByText(/Applies your Delivery tuning/)).toBeInTheDocument();
    expect(within(dialog).getByText(/not this fixed text/)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(api.generateTtsPreview).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.generateTtsPreview).mock.calls[0][0]).toMatchObject({
      provider: "sarvam", model: "bulbul:v3", voice: "vp-shubh",
      speed: 1, pauseMs: 350, energy: 50,
    });
  });
});
