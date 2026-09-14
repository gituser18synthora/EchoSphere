/* Bot lifecycle actions on the My VoiceBots page.

   Archive and Delete are two different product actions. Archive (reversible)
   calls POST /bots/{id}/archive; the bot then lives under the Archived filter,
   where Restore calls POST /bots/{id}/restore. Delete (permanent) calls
   DELETE /bots/{id} behind a type-the-name confirmation. Every action refreshes
   the list, keeps its modal open with an error toast on failure, blocks repeat
   submissions while in flight, and is hidden from roles without bots.manage. */

import { render, screen, waitFor, within } from "@testing-library/react";
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
  restoreBot: vi.fn(),
  deleteBot: vi.fn(),
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
const restoreBot = vi.mocked(api.restoreBot);
const deleteBot = vi.mocked(api.deleteBot);

const BOT: VoiceBot = {
  id: "bot_lc00000001",
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

const ARCHIVED: VoiceBot = {
  ...BOT,
  id: "bot_lc00000002",
  name: "Seasonal Promo Bot",
  status: "archived",
  liveVersion: undefined,
};

const ARCHIVE_RESULT = { archived: true, id: BOT.id, status: "archived" as const, channelsDisabled: 1, phoneNumbersReserved: 1 };
const RESTORE_RESULT = { restored: true, id: ARCHIVED.id, status: "draft" as const, phoneNumbersReassigned: 1 };
const DELETE_RESULT = { deleted: true, id: BOT.id, channelsArchived: 1, phoneNumbersReleased: 1 };

function renderPage() {
  return render(
    <MemoryRouter>
      <Bots />
    </MemoryRouter>,
  );
}

async function openMenuItem(user: ReturnType<typeof userEvent.setup>, botName: string, itemName: RegExp) {
  await screen.findByText(botName);
  const menus = screen.getAllByRole("button", { name: "More actions" });
  // Each card has one menu button; find the card that shows this bot.
  const card = screen.getByText(botName).closest(".card") as HTMLElement;
  const menu = within(card).getByRole("button", { name: "More actions" });
  expect(menus).toContain(menu);
  await user.click(menu);
  await user.click(screen.getByRole("menuitem", { name: itemName }));
}

async function showArchived(user: ReturnType<typeof userEvent.setup>) {
  await user.selectOptions(screen.getByLabelText("Filter by status"), "archived");
}

beforeEach(() => {
  grantedPermissions = new Set(["bots.manage", "costs.view"]);
  listBots.mockResolvedValue([BOT, ARCHIVED]);
  vi.mocked(api.listLanguages).mockResolvedValue([]);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("Archived filter", () => {
  it("hides archived bots from the default view and shows them under Archived", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Billing Assistant");
    expect(screen.queryByText("Seasonal Promo Bot")).not.toBeInTheDocument();
    expect(screen.getByText("1 active · 1 live · 1 archived")).toBeInTheDocument();

    await showArchived(user);
    expect(screen.getByText("Seasonal Promo Bot")).toBeInTheDocument();
    expect(screen.queryByText("Billing Assistant")).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Filter by status"), "published");
    expect(screen.getByText("Billing Assistant")).toBeInTheDocument();
    expect(screen.queryByText("Seasonal Promo Bot")).not.toBeInTheDocument();
  });

  it("explains an empty archive instead of offering to create a bot", async () => {
    const user = userEvent.setup();
    listBots.mockResolvedValue([BOT]);
    renderPage();
    await screen.findByText("Billing Assistant");
    await showArchived(user);
    expect(screen.getByText("No archived bots")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Create bot/ })).toBeInTheDocument(); // header button only
    expect(screen.getAllByRole("button", { name: /Create bot/ })).toHaveLength(1);
  });
});

describe("Archive", () => {
  it("calls the archive API, refreshes the list and closes the modal", async () => {
    const user = userEvent.setup();
    archiveBot.mockResolvedValue(ARCHIVE_RESULT);
    listBots
      .mockResolvedValueOnce([BOT, ARCHIVED])
      .mockResolvedValueOnce([{ ...BOT, status: "archived" }, ARCHIVED]);
    renderPage();
    await openMenuItem(user, "Billing Assistant", /^Archive$/);
    expect(screen.getByText(/phone number is/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Archive bot" }));

    await waitFor(() => expect(archiveBot).toHaveBeenCalledWith(BOT.id));
    expect(archiveBot).toHaveBeenCalledTimes(1);
    expect(deleteBot).not.toHaveBeenCalled();
    await waitFor(() => expect(listBots).toHaveBeenCalledTimes(2));
    expect(toast).toHaveBeenCalledWith(expect.stringContaining("archived"));
    // Now archived, the bot leaves the default (active) view; the modal is gone.
    await waitFor(() =>
      expect(screen.queryByText("Billing Assistant")).not.toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: "Archive bot" })).not.toBeInTheDocument();
  });

  it("shows the API error, keeps the modal open and does not refresh on failure", async () => {
    const user = userEvent.setup();
    archiveBot.mockRejectedValue(new Error("The database is temporarily unavailable."));
    renderPage();
    await openMenuItem(user, "Billing Assistant", /^Archive$/);
    await user.click(screen.getByRole("button", { name: "Archive bot" }));

    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith("The database is temporarily unavailable.", "error"),
    );
    expect(listBots).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Archive bot" })).toBeInTheDocument();
  });

  it("blocks repeat submissions while the request is in flight", async () => {
    const user = userEvent.setup();
    let resolveArchive: (v: typeof ARCHIVE_RESULT) => void = () => undefined;
    archiveBot.mockImplementation(
      () => new Promise<typeof ARCHIVE_RESULT>((resolve) => { resolveArchive = resolve; }),
    );
    renderPage();
    await openMenuItem(user, "Billing Assistant", /^Archive$/);
    const confirm = screen.getByRole("button", { name: "Archive bot" });
    await user.click(confirm);
    expect(archiveBot).toHaveBeenCalledTimes(1);
    expect(confirm).toBeDisabled();
    await user.click(confirm);
    expect(archiveBot).toHaveBeenCalledTimes(1);

    resolveArchive(ARCHIVE_RESULT);
    await waitFor(() => expect(listBots).toHaveBeenCalledTimes(2));
  });
});

describe("Restore", () => {
  it("offers Restore (not Archive) for an archived bot and calls the restore API", async () => {
    const user = userEvent.setup();
    restoreBot.mockResolvedValue(RESTORE_RESULT);
    renderPage();
    await screen.findByText("Billing Assistant");
    await showArchived(user);
    const card = screen.getByText("Seasonal Promo Bot").closest(".card") as HTMLElement;
    await user.click(within(card).getByRole("button", { name: "More actions" }));
    expect(screen.queryByRole("menuitem", { name: /^Archive$/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Publish center/ })).not.toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /Delete/ })).toBeInTheDocument();
    await user.click(screen.getByRole("menuitem", { name: /Restore/ }));
    expect(screen.getByText(/returns to/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Restore bot" }));

    await waitFor(() => expect(restoreBot).toHaveBeenCalledWith(ARCHIVED.id));
    expect(archiveBot).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledWith(expect.stringContaining("restored"));
    await waitFor(() => expect(listBots).toHaveBeenCalledTimes(2));
  });
});

describe("Delete", () => {
  it("requires typing the bot name before the permanent delete can be confirmed", async () => {
    const user = userEvent.setup();
    deleteBot.mockResolvedValue(DELETE_RESULT);
    listBots.mockResolvedValueOnce([BOT, ARCHIVED]).mockResolvedValueOnce([ARCHIVED]);
    renderPage();
    await openMenuItem(user, "Billing Assistant", /^Delete$/);

    expect(screen.getByText(/cannot be undone/)).toBeInTheDocument();
    expect(screen.getByText(/released back to the platform pool/)).toBeInTheDocument();
    const confirm = screen.getByRole("button", { name: "Delete bot permanently" });
    expect(confirm).toBeDisabled();

    const input = screen.getByLabelText("Type Billing Assistant to confirm");
    await user.type(input, "Billing");
    expect(confirm).toBeDisabled();
    await user.click(confirm);
    expect(deleteBot).not.toHaveBeenCalled();

    await user.clear(input);
    await user.type(input, "Billing Assistant");
    expect(confirm).toBeEnabled();
    await user.click(confirm);

    await waitFor(() => expect(deleteBot).toHaveBeenCalledWith(BOT.id));
    expect(archiveBot).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledWith(expect.stringContaining("deleted permanently"));
    await waitFor(() => expect(listBots).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.queryByText("Billing Assistant")).not.toBeInTheDocument(),
    );
  });

  it("shows the API error and keeps the modal open on failure", async () => {
    const user = userEvent.setup();
    deleteBot.mockRejectedValue(new Error("Permanent deletion is disabled in the development environment."));
    renderPage();
    await openMenuItem(user, "Billing Assistant", /^Delete$/);
    await user.type(screen.getByLabelText("Type Billing Assistant to confirm"), "Billing Assistant");
    await user.click(screen.getByRole("button", { name: "Delete bot permanently" }));

    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        "Permanent deletion is disabled in the development environment.", "error",
      ),
    );
    expect(listBots).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Delete bot permanently" })).toBeInTheDocument();
  });

  it("is available for archived bots too", async () => {
    const user = userEvent.setup();
    deleteBot.mockResolvedValue({ ...DELETE_RESULT, id: ARCHIVED.id });
    renderPage();
    await screen.findByText("Billing Assistant");
    await showArchived(user);
    await openMenuItem(user, "Seasonal Promo Bot", /^Delete$/);
    await user.type(screen.getByLabelText("Type Seasonal Promo Bot to confirm"), "Seasonal Promo Bot");
    await user.click(screen.getByRole("button", { name: "Delete bot permanently" }));
    await waitFor(() => expect(deleteBot).toHaveBeenCalledWith(ARCHIVED.id));
  });
});

describe("Permissions", () => {
  it("hides Archive and Delete without bots.manage", async () => {
    grantedPermissions = new Set(["costs.view"]);
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Billing Assistant");
    await user.click(screen.getByRole("button", { name: "More actions" }));
    expect(screen.getByRole("menuitem", { name: /View analytics/ })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /^Archive$/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /^Delete$/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Restore/ })).not.toBeInTheDocument();
  });
});
