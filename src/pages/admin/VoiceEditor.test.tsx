import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PlatformConfig from "@/pages/admin/PlatformConfig";
import { clearProviderCatalogCache } from "@/components/ProviderModelSelect";
import * as api from "@/services/api";

vi.mock("@/services/api", () => ({
  listMaster: vi.fn(),
  createMaster: vi.fn(),
  updateMaster: vi.fn(),
  deleteMaster: vi.fn(),
  duplicatePlan: vi.fn(),
  getMasterAudit: vi.fn(),
  listPlanTenants: vi.fn(),
  setMasterStatus: vi.fn(),
  getProviderCatalog: vi.fn(),
  listProviderModels: vi.fn(),
  getModelLanguages: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), hasPermission: () => true }),
}));

const listMaster = vi.mocked(api.listMaster);
const createMaster = vi.mocked(api.createMaster);
const listProviderModels = vi.mocked(api.listProviderModels);

const paged = (items: Record<string, unknown>[]) => ({
  items, meta: { page: 1, pageSize: 25, total: items.length, totalPages: 1 },
});

/* Provider schemas mirroring the real seeded catalog (ElevenLabs vs Sarvam). */
const EL_SCHEMA = {
  stability: { type: "number", min: 0, max: 1, default: 0, step: 0.05, label: "Stability" },
  similarity_boost: { type: "number", min: 0, max: 1, default: 1, step: 0.05, label: "Similarity boost" },
  style: { type: "number", min: 0, max: 1, default: 0, step: 0.05, label: "Style" },
  use_speaker_boost: { type: "boolean", default: true, label: "Speaker boost" },
  speed: { type: "number", min: 0.7, max: 1.2, default: 1, step: 0.05, label: "Speed" },
};
const SARVAM_SCHEMA = {
  pace: { type: "number", min: 0.5, max: 2, default: 1, step: 0.05, label: "Pace" },
  temperature: { type: "number", min: 0.01, max: 1, default: 0.6, step: 0.01, label: "Temperature" },
};

const MODELS: Record<string, unknown[]> = {
  elevenlabs: [{
    code: "eleven_flash_v2_5", displayName: "Eleven Flash v2.5", provider: "elevenlabs",
    capability: "tts", languages: ["en", "hi"], codecs: [], sampleRates: [], streaming: true,
    paramsSchema: EL_SCHEMA, isDefault: true,
  }],
  sarvam: [{
    code: "bulbul:v3", displayName: "Bulbul v3", provider: "sarvam",
    capability: "tts", languages: ["hi-IN"], codecs: [], sampleRates: [], streaming: true,
    paramsSchema: SARVAM_SCHEMA, isDefault: true,
  }],
};

const SAVED_VOICE = {
  id: "vp_1", name: "Monika", provider: "elevenlabs", providerVoiceId: "f1abx",
  gender: "female", locale: "", languages: [], speakingRate: 1, status: "active",
  sortOrder: 0, usageCount: 0, modelCodes: ["eleven_flash_v2_5"],
  providerSettings: { stability: 0.3, similarity_boost: 0.8, style: 0.2, use_speaker_boost: false, speed: 1.1 },
  sample: "", description: "", premium: false, isDefault: false,
};

function installMocks() {
  listMaster.mockImplementation((mtype: string, opts?: { kind?: string }) => {
    if (mtype === "voices") return Promise.resolve(paged([SAVED_VOICE]) as never);
    if (mtype === "providers" && opts?.kind === "tts") {
      return Promise.resolve(paged([
        { id: "p1", code: "elevenlabs", name: "ElevenLabs", status: "active", kind: "tts" },
        { id: "p2", code: "sarvam", name: "Sarvam AI", status: "active", kind: "tts" },
      ]) as never);
    }
    if (mtype === "providers" && opts?.kind === "voice") {
      return Promise.resolve(paged([
        { id: "p3", code: "platform", name: "Platform Voices", status: "active", kind: "voice" },
      ]) as never);
    }
    return Promise.resolve(paged([]) as never);
  });
  listProviderModels.mockImplementation((_cap, code: string) =>
    Promise.resolve((MODELS[code] ?? []) as never));
  vi.mocked(api.getProviderCatalog).mockResolvedValue({ tts: [] } as never);
  vi.mocked(api.getModelLanguages).mockResolvedValue({
    languages: [{ code: "hi-IN", name: "Hindi", nativeName: "हिन्दी" }],
    supportsAutoDetect: false, languageAgnostic: false,
  } as never);
  createMaster.mockResolvedValue({ id: "vp_new" } as never);
  vi.mocked(api.updateMaster).mockResolvedValue({ id: "vp_1" } as never);
}

async function openAddVoice(user: ReturnType<typeof userEvent.setup>) {
  render(<PlatformConfig />);
  await user.click(screen.getByText("Voices"));
  await screen.findByText("Monika");
  await user.click(screen.getByRole("button", { name: /add voice/i }));
  await screen.findByRole("dialog", { name: "Add voice" });
}

async function pickProvider(user: ReturnType<typeof userEvent.setup>, label: string) {
  await user.selectOptions(await screen.findByLabelText("TTS provider"), [label]);
}

describe("VoiceEditor — provider-specific fields", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearProviderCatalogCache();
    installMocks();
  });

  it("requires the provider first: model disabled, settings show a hint", async () => {
    const user = userEvent.setup();
    await openAddVoice(user);
    expect(screen.getByLabelText("Model")).toBeDisabled();
    expect(screen.getByText("Select a TTS provider to see its synthesis settings.")).toBeInTheDocument();
  });

  it("ElevenLabs shows its own fields and hides Sarvam's", async () => {
    const user = userEvent.setup();
    await openAddVoice(user);
    await pickProvider(user, "ElevenLabs");
    await user.selectOptions(screen.getByLabelText("Model"), ["eleven_flash_v2_5"]);
    for (const label of ["Stability", "Similarity boost", "Style", "Speaker boost", "Speed"]) {
      expect(await screen.findByText(label)).toBeInTheDocument();
    }
    expect(screen.queryByText("Pace")).not.toBeInTheDocument();
    expect(screen.getByLabelText("ElevenLabs voice ID")).toBeInTheDocument();
  });

  it("Sarvam shows its own fields and hides ElevenLabs'", async () => {
    const user = userEvent.setup();
    await openAddVoice(user);
    await pickProvider(user, "Sarvam AI");
    await user.selectOptions(screen.getByLabelText("Model"), ["bulbul:v3"]);
    expect(await screen.findByText("Pace")).toBeInTheDocument();
    expect(screen.getByText("Temperature")).toBeInTheDocument();
    expect(screen.queryByText("Stability")).not.toBeInTheDocument();
    expect(screen.queryByText("Speaker boost")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Speaker code")).toBeInTheDocument();
  });

  it("submits every ElevenLabs setting in the structured providerSettings object", async () => {
    const user = userEvent.setup();
    await openAddVoice(user);
    await pickProvider(user, "ElevenLabs");
    await user.selectOptions(screen.getByLabelText("Model"), ["eleven_flash_v2_5"]);
    await screen.findByText("Stability");

    await user.type(screen.getByLabelText("Display name"), "Test EL Voice");
    await user.type(screen.getByLabelText("ElevenLabs voice ID"), "voice123");
    const setNumber = async (label: string, value: string) => {
      const input = screen.getByLabelText(`${label} value`);
      await user.clear(input);
      await user.type(input, value);
      await user.tab();
    };
    await setNumber("Stability", "0.4");
    await setNumber("Similarity boost", "0.9");
    await setNumber("Style", "0.1");
    await setNumber("Speed", "1.1");
    await user.click(screen.getByRole("switch", { name: "Speaker boost" }));

    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(createMaster).toHaveBeenCalledTimes(1));
    expect(createMaster.mock.calls[0][1]).toMatchObject({
      name: "Test EL Voice",
      provider: "elevenlabs",
      providerVoiceId: "voice123",
      modelCodes: ["eleven_flash_v2_5"],
      providerSettings: {
        stability: 0.4, similarity_boost: 0.9, style: 0.1,
        use_speaker_boost: false, speed: 1.1,
      },
    });
  });

  it("switching provider asks for confirmation and clears incompatible values, keeping common ones", async () => {
    const user = userEvent.setup();
    await openAddVoice(user);
    await user.type(screen.getByLabelText("Display name"), "Keep me");
    await pickProvider(user, "ElevenLabs");
    await user.selectOptions(screen.getByLabelText("Model"), ["eleven_flash_v2_5"]);
    await user.type(await screen.findByLabelText("ElevenLabs voice ID"), "voice123");

    await pickProvider(user, "Sarvam AI");
    // Substantial provider data entered → confirmation before discarding.
    const confirm = await screen.findByText("Switch provider?");
    expect(confirm).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Switch provider" }));

    expect(screen.getByLabelText("Speaker code")).toHaveValue("");
    expect(screen.getByLabelText("Model")).toHaveValue("");
    expect(screen.getByLabelText("Display name")).toHaveValue("Keep me");
    expect(await screen.findByText(/provider-specific fields below were reset|were reset for sarvam/i)).toBeInTheDocument();
  });

  it("edit mode loads the saved provider-specific values", async () => {
    const user = userEvent.setup();
    render(<PlatformConfig />);
    await user.click(screen.getByText("Voices"));
    await screen.findByText("Monika");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await screen.findByRole("dialog", { name: "Edit voice" });

    expect(screen.getByLabelText("TTS provider")).toHaveValue("elevenlabs");
    await waitFor(() => expect(screen.getByLabelText("Model")).toHaveValue("eleven_flash_v2_5"));
    expect(await screen.findByLabelText("Stability value")).toHaveValue(0.3);
    expect(screen.getByLabelText("Similarity boost value")).toHaveValue(0.8);
    expect(screen.getByLabelText("Speed value")).toHaveValue(1.1);
    expect(screen.getByRole("switch", { name: "Speaker boost" })).toHaveAttribute("aria-checked", "false");
  });

  it("Reset restores provider defaults", async () => {
    const user = userEvent.setup();
    await openAddVoice(user);
    await user.type(screen.getByLabelText("Display name"), "To be reset");
    await pickProvider(user, "ElevenLabs");
    await user.click(screen.getByRole("button", { name: "Reset" }));
    expect(screen.getByLabelText("Display name")).toHaveValue("");
    expect(screen.getByLabelText("TTS provider")).toHaveValue("");
  });

  it("a failed save keeps the entered form data", async () => {
    createMaster.mockRejectedValue(Object.assign(new Error("Validation failed."), {
      fieldErrors: { providerSettings: "Settings: 'stability' must be between 0 and 1." },
    }));
    const user = userEvent.setup();
    await openAddVoice(user);
    await pickProvider(user, "ElevenLabs");
    await user.selectOptions(screen.getByLabelText("Model"), ["eleven_flash_v2_5"]);
    await user.type(screen.getByLabelText("Display name"), "Persistent");
    await user.type(await screen.findByLabelText("ElevenLabs voice ID"), "v1");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText("Validation failed.")).toBeInTheDocument();
    expect(screen.getByText(/must be between 0 and 1/)).toBeInTheDocument();
    expect(screen.getByLabelText("Display name")).toHaveValue("Persistent");
  });
});
