/* Bot Clone action on the My VoiceBots page.

   The action must call the real clone API (no simulated success), refresh the
   list, surface the cloned bot's name on success and the API error on failure,
   stay disabled while a clone is in flight, and be invisible to roles without
   the bots.manage permission. */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Bots from "@/pages/tenant/Bots";
import * as api from "@/services/api";
import type { VoiceBot } from "@/types/domain";

vi.mock("@/services/api", () => ({
  listBots: vi.fn(),
  createBot: vi.fn(),
  cloneBot: vi.fn(),
  listLanguages: vi.fn(),
  simulateAction: vi.fn(),
}));

const toast = vi.fn();
let grantedPermissions = new Set<string>(["bots.manage", "costs.view"]);
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({
    toast,
    hasPermission: (code: string) => grantedPermissions.has(code),
  }),
}));

const listBots = vi.mocked(api.listBots);
const cloneBot = vi.mocked(api.cloneBot);

const BOT: VoiceBot = {
  id: "bot_src000001",
  tenantId: "tn_test",
  name: "Billing Assistant",
  useCase: "Billing support",
  description: "",
  languages: ["en-IN"],
  status: "published",
  version: "v2.3.0",
  liveVersion: "v2.3.0",
  owner: "Asha",
  health: "good",
  containment: 70,
  callsToday: 3,
  callsMonth: 120,
  avgCostPerCall: 0.4,
  csat: 4.5,
  channels: ["voice"],
  guardrailProfileId: "",
  updatedAt: "2026-08-20T10:00:00Z",
  readiness: [],
};

const CLONED: VoiceBot = {
  ...BOT,
  id: "bot_clone00001",
  name: "Billing Assistant (copy)",
  status: "draft",
  liveVersion: undefined,
  channels: [],
  callsToday: 0,
  callsMonth: 0,
};

function renderPage() {
  return render(
    <MemoryRouter>
      <Bots />
    </MemoryRouter>,
  );
}

async function openBotMenu(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByText("Billing Assistant");
  await user.click(screen.getByRole("button", { name: "More actions" }));
}

beforeEach(() => {
  grantedPermissions = new Set(["bots.manage", "costs.view"]);
  listBots.mockResolvedValue([BOT]);
  vi.mocked(api.listLanguages).mockResolvedValue([]);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("Clone bot action", () => {
  it("calls the clone API, refreshes the list and toasts the cloned name", async () => {
    const user = userEvent.setup();
    cloneBot.mockResolvedValue(CLONED);
    renderPage();
    await openBotMenu(user);
    await user.click(screen.getByRole("menuitem", { name: /Clone bot/ }));

    await waitFor(() => expect(cloneBot).toHaveBeenCalledWith(BOT.id));
    expect(cloneBot).toHaveBeenCalledTimes(1);
    // Initial load + the refresh after a successful clone.
    await waitFor(() => expect(listBots).toHaveBeenCalledTimes(2));
    expect(toast).toHaveBeenCalledWith(
      expect.stringContaining("Billing Assistant (copy)"),
    );
  });

  it("shows the API error and does not refresh the list on failure", async () => {
    const user = userEvent.setup();
    cloneBot.mockRejectedValue(new Error("The record conflicts with existing data."));
    renderPage();
    await openBotMenu(user);
    await user.click(screen.getByRole("menuitem", { name: /Clone bot/ }));

    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        "The record conflicts with existing data.", "error",
      ),
    );
    expect(listBots).toHaveBeenCalledTimes(1);
  });

  it("disables the action while a clone is in flight", async () => {
    const user = userEvent.setup();
    let resolveClone: (bot: VoiceBot) => void = () => undefined;
    cloneBot.mockImplementation(
      () => new Promise<VoiceBot>((resolve) => { resolveClone = resolve; }),
    );
    renderPage();
    await openBotMenu(user);
    await user.click(screen.getByRole("menuitem", { name: /Clone bot/ }));
    expect(cloneBot).toHaveBeenCalledTimes(1);

    // Re-open the menu while the request is pending: the action is disabled
    // and clicking it again must not fire a second request.
    await user.click(screen.getByRole("button", { name: "More actions" }));
    const pending = await screen.findByRole("menuitem", { name: /Cloning…/ });
    expect(pending).toBeDisabled();
    await user.click(pending);
    expect(cloneBot).toHaveBeenCalledTimes(1);

    resolveClone(CLONED);
    await waitFor(() => expect(listBots).toHaveBeenCalledTimes(2));
  });

  it("is hidden without the bots.manage permission", async () => {
    grantedPermissions = new Set(["costs.view"]);
    const user = userEvent.setup();
    renderPage();
    await openBotMenu(user);
    expect(screen.getByRole("menuitem", { name: /View analytics/ })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Clone bot/ })).not.toBeInTheDocument();
  });
});
