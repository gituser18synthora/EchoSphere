import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { VoiceCapability } from "@/types/domain";
import {
  clearProviderCatalogCache, ModelSelect, ProviderSelect,
} from "@/components/ProviderModelSelect";
import * as api from "@/services/api";

vi.mock("@/services/api", () => ({
  getProviderCatalog: vi.fn(),
  listProviderModels: vi.fn(),
}));

const getProviderCatalog = vi.mocked(api.getProviderCatalog);
const listProviderModels = vi.mocked(api.listProviderModels);

const CATALOG: Record<string, { code: string; name: string }[]> = {
  llm: [{ code: "openai", name: "OpenAI" }, { code: "mock", name: "Mock LLM (dev)" }],
  stt: [{ code: "sarvam", name: "Sarvam AI" }, { code: "deepgram", name: "Deepgram" }],
  tts: [{ code: "sarvam", name: "Sarvam AI" }, { code: "elevenlabs", name: "ElevenLabs" }],
  embedding: [{ code: "openai", name: "OpenAI Embeddings" }],
};

const MODELS: Record<string, { code: string; displayName: string; isDefault: boolean }[]> = {
  "llm:openai": [
    { code: "gpt-4o-mini", displayName: "GPT-4o mini", isDefault: true },
    { code: "gpt-4o", displayName: "GPT-4o", isDefault: false },
  ],
  "llm:mock": [{ code: "mock", displayName: "Mock LLM", isDefault: true }],
  "stt:sarvam": [{ code: "saaras:v3", displayName: "Saaras v3", isDefault: true }],
  "stt:deepgram": [],
  "tts:sarvam": [{ code: "bulbul:v3", displayName: "Bulbul v3", isDefault: true }],
  "tts:elevenlabs": [{ code: "eleven_flash_v2_5", displayName: "Eleven Flash v2.5", isDefault: true }],
  "embedding:openai": [{ code: "text-embedding-3-small", displayName: "text-embedding-3-small", isDefault: true }],
};

function installMocks() {
  getProviderCatalog.mockImplementation((capability) =>
    Promise.resolve({ [capability as string]: CATALOG[capability as string] ?? [] } as never));
  listProviderModels.mockImplementation((capability, code) =>
    Promise.resolve((MODELS[`${capability}:${code}`] ?? []) as never));
}

/** Provider + dependent model pair, wired the way forms use it. */
function Pair({ capability }: { capability: VoiceCapability }) {
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  return (
    <>
      <ProviderSelect capability={capability} value={provider} label="Provider"
        onChange={(code) => { setProvider(code); setModel(""); }} />
      <ModelSelect capability={capability} provider={provider} value={model} label="Model"
        onChange={setModel} />
    </>
  );
}

describe("ProviderModelSelect", () => {
  beforeEach(() => {
    clearProviderCatalogCache();
    getProviderCatalog.mockReset();
    listProviderModels.mockReset();
    installMocks();
  });

  it("model select is disabled until a provider is chosen", async () => {
    render(<Pair capability="llm" />);
    const model = screen.getByLabelText("Model");
    expect(model).toBeDisabled();
    expect(screen.getByText("Select a provider first")).toBeInTheDocument();
  });

  it.each([
    ["llm", "OpenAI", "GPT-4o mini · default"],
    ["stt", "Sarvam AI", "Saaras v3 · default"],
    ["tts", "ElevenLabs", "Eleven Flash v2.5 · default"],
    ["embedding", "OpenAI Embeddings", "text-embedding-3-small · default"],
  ] as const)("%s provider filters its model list", async (capability, providerName, modelLabel) => {
    const user = userEvent.setup();
    render(<Pair capability={capability} />);
    await user.selectOptions(await screen.findByLabelText("Provider"), [providerName]);
    expect(await screen.findByText(modelLabel)).toBeInTheDocument();
    expect(listProviderModels).toHaveBeenCalledWith(capability, expect.any(String));
  });

  it("only shows models belonging to the selected provider", async () => {
    const user = userEvent.setup();
    render(<Pair capability="tts" />);
    await user.selectOptions(await screen.findByLabelText("Provider"), ["Sarvam AI"]);
    expect(await screen.findByText("Bulbul v3 · default")).toBeInTheDocument();
    expect(screen.queryByText(/Eleven Flash/)).not.toBeInTheDocument();
  });

  it("changing provider clears an incompatible model", async () => {
    const user = userEvent.setup();
    render(<Pair capability="llm" />);
    const provider = await screen.findByLabelText("Provider");
    await user.selectOptions(provider, ["OpenAI"]);
    const model = screen.getByLabelText("Model");
    await user.selectOptions(model, [await screen.findByText("GPT-4o mini · default")]);
    expect(model).toHaveValue("gpt-4o-mini");
    await user.selectOptions(provider, ["Mock LLM (dev)"]);
    await waitFor(() => expect(model).toHaveValue(""));
  });

  it("shows an empty state when the provider has no configured models", async () => {
    const user = userEvent.setup();
    render(<Pair capability="stt" />);
    await user.selectOptions(await screen.findByLabelText("Provider"), ["Deepgram"]);
    expect(await screen.findByText("No models configured for this provider")).toBeInTheDocument();
    expect(screen.getByLabelText("Model")).toBeDisabled();
  });

  it("shows a loading state while models are fetched", async () => {
    let resolve!: (models: never) => void;
    listProviderModels.mockImplementation(() => new Promise((r) => { resolve = r; }));
    const user = userEvent.setup();
    render(<Pair capability="llm" />);
    await user.selectOptions(await screen.findByLabelText("Provider"), ["OpenAI"]);
    expect(screen.getByText("Loading models…")).toBeInTheDocument();
    resolve(MODELS["llm:openai"] as never);
    expect(await screen.findByText("GPT-4o mini · default")).toBeInTheDocument();
  });

  it("keeps an inactive saved model visible in edit mode with an unavailable warning", async () => {
    function EditHarness() {
      const [model, setModel] = useState("tts-legacy");
      return <ModelSelect capability="tts" provider="sarvam" value={model} label="Model" onChange={setModel} />;
    }
    render(<EditHarness />);
    expect(await screen.findByText("tts-legacy (unavailable — inactive)")).toBeInTheDocument();
    expect(screen.getByLabelText("Model")).toHaveValue("tts-legacy");
  });
});
