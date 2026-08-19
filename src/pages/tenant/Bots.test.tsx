import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Bots from "@/pages/tenant/Bots";
import * as api from "@/services/api";

vi.mock("@/services/api", () => ({
  listBots: vi.fn(),
  listLanguages: vi.fn(),
  createBot: vi.fn(),
  simulateAction: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), hasPermission: () => true }),
}));

describe("Bots — create language defaults", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listBots).mockResolvedValue([]);
    vi.mocked(api.listLanguages).mockResolvedValue([
      { id: "lang-us", code: "en-US", name: "English (US)", enabled: false, isDefault: true },
      { id: "lang-in", code: "en-IN", name: "English (India)", enabled: true, isDefault: false },
      { id: "lang-hi", code: "hi-IN", name: "Hindi", enabled: true, isDefault: false },
    ]);
  });

  it("never preselects an inactive former default", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><Bots /></MemoryRouter>);

    await user.click((await screen.findAllByRole("button", { name: "Create bot" }))[0]);
    const dialog = await screen.findByRole("dialog", { name: "Create a VoiceBot" });

    await waitFor(() => {
      expect(within(dialog).getByRole("button", { name: "Remove English (India)" })).toBeInTheDocument();
    });
    expect(within(dialog).queryByText("en-US")).not.toBeInTheDocument();
  });
});
