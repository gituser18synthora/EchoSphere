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
        { code: "bulbul:v3", displayName: "Bulbul v3", isDefault: true },
        { code: "bulbul:v2", displayName: "Bulbul v2", isDefault: false },
      ] : provider === "elevenlabs" ? [
        { code: "eleven_flash_v2_5", displayName: "Eleven Flash v2.5", isDefault: true },
      ] : [],
    )) as never);
  vi.mocked(api.listProviderVoices).mockImplementation(((provider: string) =>
    Promise.resolve(
      provider === "sarvam" ? [
        voice("vp-shubh", "Shubh", ["en-IN", "hi-IN"], ["bulbul:v3", "bulbul:v2"]),
        voice("vp-anushka", "Anushka", ["hi-IN"], ["bulbul:v3"]),
      ] : provider === "elevenlabs" ? [
        voice("vp-rachel", "Rachel", ["en-IN"], ["eleven_flash_v2_5"], "elevenlabs"),
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
