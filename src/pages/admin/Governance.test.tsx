import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Governance from "@/pages/admin/Governance";
import * as api from "@/services/api";

vi.mock("@/services/api", () => ({
  listMaster: vi.fn(),
  setMasterStatus: vi.fn(),
  listModels: vi.fn(),
  updateModelStatus: vi.fn(),
  listGuardrails: vi.fn(),
  updateGuardrail: vi.fn(),
  listTemplates: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), hasPermission: () => true }),
}));

const listMaster = vi.mocked(api.listMaster);
const setMasterStatus = vi.mocked(api.setMasterStatus);

const paged = (items: Record<string, unknown>[]) => ({
  items,
  meta: { page: 1, pageSize: 100, total: items.length, totalPages: 1 },
});

const PROVIDERS: Record<string, Record<string, unknown>[]> = {
  llm: [
    { id: "prov_llm_openai", code: "openai", name: "OpenAI", status: "active", kind: "llm", usageCount: 3 },
    { id: "prov_llm_anthropic", code: "anthropic", name: "Anthropic", status: "inactive", kind: "llm", usageCount: 0 },
  ],
  stt: [
    { id: "prov_stt_sarvam", code: "sarvam", name: "Sarvam AI", status: "active", kind: "stt", usageCount: 2 },
    { id: "prov_stt_openai", code: "openai", name: "OpenAI Whisper", status: "inactive", kind: "stt", usageCount: 1 },
  ],
};

const MODELS: Record<string, Record<string, unknown>[]> = {
  "llm:openai": [
    { id: "pm_1", code: "gpt-4o-mini", name: "GPT-4o mini", displayName: "GPT-4o mini",
      providerCode: "openai", capability: "llm", status: "active", isDefault: true, usageCount: 2 },
    { id: "pm_2", code: "gpt-4o", name: "GPT-4o", displayName: "GPT-4o",
      providerCode: "openai", capability: "llm", status: "active", isDefault: false, usageCount: 1 },
  ],
  "stt:sarvam": [
    { id: "pm_3", code: "saaras:v3", name: "Saaras v3", displayName: "Saaras v3 (streaming)",
      providerCode: "sarvam", capability: "stt", status: "active", isDefault: true, usageCount: 2 },
  ],
};

function installMocks() {
  listMaster.mockImplementation(((mtype: string, opts?: { kind?: string; capability?: string; provider?: string }) => {
    if (mtype === "providers") return Promise.resolve(paged(PROVIDERS[opts?.kind ?? ""] ?? []));
    if (mtype === "provider-models") {
      return Promise.resolve(paged(MODELS[`${opts?.capability}:${opts?.provider}`] ?? []));
    }
    return Promise.resolve(paged([]));
  }) as never);
  setMasterStatus.mockResolvedValue({} as never);
  vi.mocked(api.listModels).mockResolvedValue([] as never);
  vi.mocked(api.listGuardrails).mockResolvedValue([] as never);
  vi.mocked(api.listTemplates).mockResolvedValue([] as never);
}

beforeEach(() => {
  vi.clearAllMocks();
  installMocks();
});

describe("AI Governance — provider matrix", () => {
  it("renders DB-driven providers for the default LLM capability with status chips", async () => {
    render(<Governance />);
    expect(await screen.findByText("OpenAI")).toBeInTheDocument();
    expect(screen.getByText("Anthropic")).toBeInTheDocument();
    expect(listMaster).toHaveBeenCalledWith("providers", expect.objectContaining({ kind: "llm" }));
    // Inactive providers stay visible in the governance console (not dropdowns).
    const anthropicRow = screen.getByText("Anthropic").closest("tr")!;
    expect(within(anthropicRow).getByText(/inactive/i)).toBeInTheDocument();
  });

  it("loads the models of the selected provider and capability", async () => {
    render(<Governance />);
    expect(await screen.findByText("GPT-4o mini")).toBeInTheDocument();
    expect(listMaster).toHaveBeenCalledWith(
      "provider-models",
      expect.objectContaining({ capability: "llm", provider: "openai" }),
    );
  });

  it("switches capability and fetches that capability's providers and models", async () => {
    const user = userEvent.setup();
    render(<Governance />);
    await screen.findByText("OpenAI");
    await user.click(screen.getByRole("button", { name: "Speech-to-Text" }));
    expect(await screen.findByText("Sarvam AI")).toBeInTheDocument();
    expect(listMaster).toHaveBeenCalledWith("providers", expect.objectContaining({ kind: "stt" }));
    // The models panel follows the first ACTIVE provider of the capability.
    expect(await screen.findByText("Saaras v3 (streaming)")).toBeInTheDocument();
  });

  it("deactivates a provider through the audited status endpoint", async () => {
    const user = userEvent.setup();
    render(<Governance />);
    const openaiRow = (await screen.findByText("OpenAI")).closest("tr")!;
    await user.click(within(openaiRow).getByRole("button", { name: "Deactivate" }));
    await waitFor(() =>
      expect(setMasterStatus).toHaveBeenCalledWith("providers", "prov_llm_openai", "inactive"));
  });

  it("activates an inactive provider", async () => {
    const user = userEvent.setup();
    render(<Governance />);
    const anthropicRow = (await screen.findByText("Anthropic")).closest("tr")!;
    await user.click(within(anthropicRow).getByRole("button", { name: "Activate" }));
    await waitFor(() =>
      expect(setMasterStatus).toHaveBeenCalledWith("providers", "prov_llm_anthropic", "active"));
  });

  it("toggles provider-model status through the audited status endpoint", async () => {
    const user = userEvent.setup();
    render(<Governance />);
    const modelRow = (await screen.findByText("GPT-4o")).closest("tr")!;
    await user.click(within(modelRow).getByRole("button", { name: "Deactivate" }));
    await waitFor(() =>
      expect(setMasterStatus).toHaveBeenCalledWith("provider-models", "pm_2", "inactive"));
  });

  it("moves a deactivated provider model to the bottom after the refetch", async () => {
    const user = userEvent.setup();
    let flipped = false;
    // Active-first server ordering: once GPT-4o mini is inactive it returns last.
    const REORDERED = [
      { ...MODELS["llm:openai"][1] },                      // GPT-4o (active) → first
      { ...MODELS["llm:openai"][0], status: "inactive" },  // GPT-4o mini (inactive) → last
    ];
    listMaster.mockImplementation(((mtype: string, opts?: { kind?: string; capability?: string; provider?: string }) => {
      if (mtype === "providers") return Promise.resolve(paged(PROVIDERS[opts?.kind ?? ""] ?? []));
      if (mtype === "provider-models") {
        const key = `${opts?.capability}:${opts?.provider}`;
        if (key === "llm:openai") return Promise.resolve(paged(flipped ? REORDERED : MODELS[key]));
        return Promise.resolve(paged(MODELS[key] ?? []));
      }
      return Promise.resolve(paged([]));
    }) as never);
    setMasterStatus.mockImplementation(() => { flipped = true; return Promise.resolve({} as never); });

    render(<Governance />);
    await screen.findByText("GPT-4o mini");
    const modelOrder = () => screen.getAllByRole("row")
      .map((r) => within(r).queryByText("GPT-4o mini") ? "mini"
        : within(r).queryByText("GPT-4o") ? "gpt4o" : null)
      .filter(Boolean);
    expect(modelOrder()).toEqual(["mini", "gpt4o"]);

    const miniRow = screen.getByText("GPT-4o mini").closest("tr")!;
    await user.click(within(miniRow).getByRole("button", { name: "Deactivate" }));
    await waitFor(() =>
      expect(setMasterStatus).toHaveBeenCalledWith("provider-models", "pm_1", "inactive"));
    // No manual refresh: the models table refetched and the inactive model dropped last.
    await waitFor(() => expect(modelOrder()).toEqual(["gpt4o", "mini"]));
  });
});
