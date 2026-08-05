import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PromptsTab from "@/pages/tenant/studio/PromptsTab";
import * as api from "@/services/api";

vi.mock("@/services/api", () => ({
  listPrompts: vi.fn(),
  listLanguages: vi.fn(),
  savePromptVersion: vi.fn(),
  updatePrompt: vi.fn(),
  createPrompt: vi.fn(),
  deletePrompt: vi.fn(),
  duplicatePrompt: vi.fn(),
  compilePromptPreview: vi.fn(),
  testPrompt: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), hasPermission: () => true }),
}));

const BOT = {
  id: "bot-prompt", tenantId: "tn-prompt", name: "Prompt Bot",
  languages: ["en-IN", "hi-IN"],
};
const PROMPT = {
  id: "pr-greeting", botId: BOT.id, tenantId: BOT.tenantId,
  type: "greeting", name: "Opening greeting", description: "Welcome callers",
  variables: [], state: "draft", activeVersion: 1, publishedVersion: null,
  versions: [{
    version: 1, editedAt: "2026-08-01T00:00:00Z", editedBy: "Admin",
    note: "Initial", promptMode: "structured", structuredConfig: null,
    fullPrompt: null, compiledPrompt: null,
    variants: [
      { language: "en-IN", content: "Hello" },
      { language: "hi-IN", content: "नमस्ते" },
    ],
  }],
};

describe("PromptsTab — tenant-scoped removable language variants", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listPrompts).mockResolvedValue([PROMPT] as never);
    vi.mocked(api.listLanguages).mockResolvedValue([
      { id: "en", code: "en-IN", name: "English (India)", enabled: true },
      { id: "hi", code: "hi-IN", name: "Hindi", enabled: true },
      { id: "mr", code: "mr-IN", name: "Marathi", enabled: true },
    ]);
    vi.mocked(api.savePromptVersion).mockResolvedValue(PROMPT as never);
  });

  it("loads only tenant languages and removes an existing variant", async () => {
    const user = userEvent.setup();
    render(<PromptsTab bot={BOT as never} />);

    await user.click(await screen.findByText("Opening greeting"));
    const drawer = await screen.findByRole("dialog");
    const addLanguage = within(drawer).getByLabelText("Add language");

    expect(api.listLanguages).toHaveBeenCalledWith(false, "tn-prompt");
    expect(within(addLanguage).getByRole("option", { name: "Marathi (mr-IN)" })).toBeInTheDocument();
    expect(within(addLanguage).queryByRole("option", { name: /en-US/ })).not.toBeInTheDocument();

    await user.click(within(drawer).getByRole("button", { name: "Remove Hindi (hi-IN)" }));
    await user.click(within(drawer).getByRole("button", { name: "Save draft" }));

    await waitFor(() => {
      expect(api.savePromptVersion).toHaveBeenCalledWith("pr-greeting", expect.objectContaining({
        variants: [{ language: "en-IN", content: "Hello" }],
      }));
    });
  });

  it("provides a large content editor while creating a full prompt", async () => {
    const user = userEvent.setup();
    vi.mocked(api.createPrompt).mockResolvedValue(PROMPT as never);
    render(<PromptsTab bot={BOT as never} />);

    await user.click(screen.getByRole("button", { name: "New prompt" }));
    const modal = await screen.findByRole("dialog", { name: "New prompt" });
    await user.selectOptions(within(modal).getByLabelText("Prompt type"), "system:full");

    const content = within(modal).getByLabelText("Full prompt content");
    expect(content).toHaveStyle({ minHeight: "320px" });
    const completePrompt = `${"Detailed collection instructions. ".repeat(30)}\n# Closing\nThank the caller.`;
    fireEvent.change(content, { target: { value: completePrompt } });
    await user.type(within(modal).getByLabelText("Name"), "Collections unified prompt");
    await user.click(within(modal).getByRole("button", { name: "Create & open editor" }));

    await waitFor(() => {
      expect(api.createPrompt).toHaveBeenCalledWith(BOT.id, expect.objectContaining({
        type: "system",
        promptMode: "full",
        fullPrompt: completePrompt,
      }));
    });
  });
});
