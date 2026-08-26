/* Bot Archive (delete) action on the My VoiceBots page.

   The confirm modal must call the real DELETE API (no simulated success),
   refresh the list so the bot disappears, keep the modal open with an error
   toast on failure, block repeat submissions while the request is in flight,
   restore status-archived bots through the real PATCH, and stay hidden from
   roles without the bots.manage permission. */

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
  archiveBot: vi.fn(),
  updateBot: vi.fn(),
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
const archiveBot = vi.mocked(api.archiveBot);
const updateBot = vi.mocked(api.updateBot);

const BOT: VoiceBot = {
  id: "bot_del0000001",
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

function renderPage() {
  return render(
    <MemoryRouter>
      <Bots />
    </MemoryRouter>,
  );
}

async function openArchiveModal(user: ReturnType<typeof userEvent.setup>, itemName: RegExp) {
  await screen.findByText("Billing Assistant");
  await user.click(screen.getByRole("button", { name: "More actions" }));
  await user.click(screen.getByRole("menuitem", { name: itemName }));
}

beforeEach(() => {
  grantedPermissions = new Set(["bots.manage", "costs.view"]);
  listBots.mockResolvedValue([BOT]);
  vi.mocked(api.listLanguages).mockResolvedValue([]);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("Archive (delete) bot action", () => {
  it("calls the delete API, refreshes the list and closes the modal", async () => {
    const user = userEvent.setup();
    archiveBot.mockResolvedValue({ archived: true });
    // After the delete, the refreshed list no longer contains the bot.
    listBots.mockResolvedValueOnce([BOT]).mockResolvedValueOnce([]);
    renderPage();
    await openArchiveModal(user, /Archive/);
    await user.click(screen.getByRole("button", { name: "Archive bot" }));

    await waitFor(() => expect(archiveBot).toHaveBeenCalledWith(BOT.id));
    expect(archiveBot).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(listBots).toHaveBeenCalledTimes(2));
    expect(toast).toHaveBeenCalledWith(expect.stringContaining("archived"));
    // The bot disappeared from the refreshed list; the modal is gone.
    await waitFor(() =>
      expect(screen.queryByText("Billing Assistant")).not.toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: "Archive bot" })).not.toBeInTheDocument();
  });

  it("shows the API error, keeps the modal open and does not refresh on failure", async () => {
    const user = userEvent.setup();
    archiveBot.mockRejectedValue(new Error("The database is temporarily unavailable."));
    renderPage();
    await openArchiveModal(user, /Archive/);
    await user.click(screen.getByRole("button", { name: "Archive bot" }));

    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        "The database is temporarily unavailable.", "error",
      ),
    );
    expect(listBots).toHaveBeenCalledTimes(1);
    // Still open for retry or cancel.
    expect(screen.getByRole("button", { name: "Archive bot" })).toBeInTheDocument();
  });

  it("blocks repeat submissions while the delete is in flight", async () => {
    const user = userEvent.setup();
    let resolveDelete: (v: { archived: boolean }) => void = () => undefined;
    archiveBot.mockImplementation(
      () => new Promise<{ archived: boolean }>((resolve) => { resolveDelete = resolve; }),
    );
    renderPage();
    await openArchiveModal(user, /Archive/);
    const confirm = screen.getByRole("button", { name: "Archive bot" });
    await user.click(confirm);
    expect(archiveBot).toHaveBeenCalledTimes(1);

    expect(confirm).toBeDisabled();
    await user.click(confirm);
    expect(archiveBot).toHaveBeenCalledTimes(1);

    resolveDelete({ archived: true });
    await waitFor(() => expect(listBots).toHaveBeenCalledTimes(2));
  });

  it("restores an archived bot to draft through the real API", async () => {
    const user = userEvent.setup();
    listBots.mockResolvedValue([{ ...BOT, status: "archived", liveVersion: undefined }]);
    updateBot.mockResolvedValue({ ...BOT, status: "draft" });
    renderPage();
    await openArchiveModal(user, /Restore/);
    await user.click(screen.getByRole("button", { name: "Restore bot" }));

    await waitFor(() =>
      expect(updateBot).toHaveBeenCalledWith(BOT.id, { status: "draft" }),
    );
    expect(toast).toHaveBeenCalledWith(expect.stringContaining("restored"));
    await waitFor(() => expect(listBots).toHaveBeenCalledTimes(2));
  });

  it("is hidden without the bots.manage permission", async () => {
    grantedPermissions = new Set(["costs.view"]);
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Billing Assistant");
    await user.click(screen.getByRole("button", { name: "More actions" }));
    expect(screen.getByRole("menuitem", { name: /View analytics/ })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Archive/ })).not.toBeInTheDocument();
  });
});
