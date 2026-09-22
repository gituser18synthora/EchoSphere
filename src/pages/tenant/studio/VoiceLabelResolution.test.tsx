/* The voice selector must never present a raw voice id as a name, and must
   never drop a valid selection while the catalog is still loading.

   Each test is one reported symptom:
   - a valid voice rendered as "<raw id> (unavailable)" during async load;
   - a voice stored under its PROVIDER WIRE id (both id forms are persisted by
     different code paths) rendered as unavailable instead of by name;
   - a genuinely missing voice must still be called out explicitly. */

import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import VoiceTab from "@/pages/tenant/studio/VoiceTab";
import type { VoiceBot } from "@/types/domain";
import * as api from "@/services/api";

vi.mock("@/services/api", () => ({
  getVoiceSettings: vi.fn(), getProviderCatalog: vi.fn(), listLanguages: vi.fn(),
  listProviderModels: vi.fn(), listProviderVoices: vi.fn(), getModelLanguages: vi.fn(),
  getRuntimeContext: vi.fn(), saveVoiceSettings: vi.fn(), validateVoiceConfig: vi.fn(),
  testProviderConnection: vi.fn(), generateTtsPreview: vi.fn(), listPrompts: vi.fn(),
  listPronunciationDictionaries: vi.fn(), getPronunciationDictionary: vi.fn(),
  createPronunciationDictionary: vi.fn(), updatePronunciationDictionary: vi.fn(),
  deletePronunciationDictionary: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), hasPermission: () => true }),
}));

const BOT = { id: "bot_1", languages: ["hi-IN"] } as unknown as VoiceBot;
const WIRE = "f1abxvIEijusskcPWE5x";

const MONIKA = {
  id: "vp-el-monika", name: "Monika", gender: "female", provider: "elevenlabs",
  providerVoiceId: WIRE, languages: [], modelCodes: ["eleven_v3_conversational"],
  locale: "hi-IN", premium: true, isDefault: false, status: "active",
  providerSettings: {}, sampleText: null,
};

const MODELS = [{
  code: "eleven_v3_conversational", displayName: "Eleven v3 Conversational",
  isDefault: false, capability: "tts", streaming: true, speedRange: null,
  paramsSchema: { stability: { type: "enum", values: [0, 0.5, 1], default: 0.5, label: "Stability" } },
}];

/** `voices` controls how listProviderVoices resolves: a pending promise models
    the loading window, an array models the catalog having returned. */
function install(voices: "pending" | unknown[]) {
  vi.mocked(api.getVoiceSettings).mockResolvedValue({
    speed: 1, pauseMs: 350, empathy: 50, energy: 50,
    sttProvider: "", sttModel: "", sttLanguage: "", sttSettings: {},
    llmProvider: "", llmModel: "", llmSettings: {},
    ttsProvider: "elevenlabs", ttsModel: "eleven_v3_conversational",
    ttsVoice: WIRE,                      // stored as the WIRE id, not vp-el-*
    ttsSettings: { stability: 0.5 },
    fallbackProvider: "", fallbackModel: "", fallbackVoice: "",
    languageVoiceMap: { default: "hi-IN" },
    audioSettings: { browser: { codec: "linear16", sampleRate: 24000 },
                     telephony: { codec: "mulaw", sampleRate: 8000 } },
  } as never);
  vi.mocked(api.getProviderCatalog).mockResolvedValue({
    stt: [], llm: [],
    tts: [{ code: "elevenlabs", name: "ElevenLabs", capability: "tts", description: "",
            requiresApiKey: true, hasCredentials: true }],
  } as never);
  vi.mocked(api.listLanguages).mockResolvedValue(
    [{ id: "l2", code: "hi-IN", name: "Hindi", enabled: true }] as never);
  vi.mocked(api.listPrompts).mockResolvedValue([] as never);
  vi.mocked(api.getRuntimeContext).mockResolvedValue(null as never);
  vi.mocked(api.listProviderModels).mockResolvedValue(MODELS as never);
  vi.mocked(api.getModelLanguages).mockResolvedValue(
    { languages: [], supportsAutoDetect: false, languageAgnostic: true } as never);
  vi.mocked(api.listPronunciationDictionaries).mockResolvedValue([] as never);
  vi.mocked(api.listProviderVoices).mockImplementation(((() =>
    voices === "pending" ? new Promise(() => {}) : Promise.resolve(voices)) as never));
}

beforeEach(() => { vi.clearAllMocks(); });

async function voiceField() {
  // The voice selector renders its current label as button text.
  return await screen.findByLabelText("TTS voice");
}

describe("voice label resolution", () => {
  it("never shows the raw id as unavailable while voices are loading", async () => {
    install("pending");
    render(<VoiceTab bot={BOT} />);
    const field = await voiceField();
    await waitFor(() => expect(field).toBeInTheDocument());
    expect(field.textContent ?? "").not.toMatch(/unavailable/i);
  });

  it("shows the readable name when the selection is stored as the wire id", async () => {
    install([MONIKA]);
    render(<VoiceTab bot={BOT} />);
    const field = await voiceField();
    await waitFor(() => expect(field.textContent ?? "").toContain("Monika"));
    expect(field.textContent ?? "").not.toMatch(/unavailable/i);
    expect(field.textContent ?? "").not.toContain(WIRE);
  });

  it("still flags a voice that is genuinely absent from the catalog", async () => {
    install([]);
    render(<VoiceTab bot={BOT} />);
    const field = await voiceField();
    await waitFor(() => expect(field.textContent ?? "").toMatch(/unavailable/i));
  });
});
