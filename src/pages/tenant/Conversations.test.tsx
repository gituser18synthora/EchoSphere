import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Conversations from "@/pages/tenant/Conversations";
import * as api from "@/services/api";
import * as exportApi from "@/services/exportDownload";

vi.mock("@/services/api", () => ({
  listConversations: vi.fn(),
  flagConversation: vi.fn(),
  simulateAction: vi.fn(),
}));
vi.mock("@/services/exportDownload", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/exportDownload")>();
  return {
    ...actual,
    downloadOperationalExport: vi.fn(),
    downloadConversationTranscript: vi.fn(),
  };
});
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn() }),
}));

const CONVERSATION = {
  id: "cv-001",
  botId: "bot-001",
  bot: "Billing Bot",
  channel: "voice",
  caller: "+91 ••• 1234",
  startedAt: "2026-07-24T10:00:00Z",
  durationSec: 75,
  sentiment: "negative",
  intents: ["billing_dispute"],
  contained: false,
  escalationReason: "API timeout",
  csat: 2,
  costUsd: 0.12,
  language: "en-IN",
  qaScore: 60,
  flagged: true,
  transcript: [],
};

describe("Tenant conversation downloads", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listConversations).mockResolvedValue([CONVERSATION] as never);
    vi.mocked(exportApi.downloadOperationalExport)
      .mockResolvedValue("conversations.xlsx");
    vi.mocked(exportApi.downloadConversationTranscript)
      .mockResolvedValue("transcript.csv");
  });

  it("exports current filters and offers CSV/Excel transcript actions", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><Conversations /></MemoryRouter>);
    expect((await screen.findAllByText("Billing Bot")).length).toBeGreaterThan(0);

    await user.click(screen.getByRole("tab", { name: /Escalated/ }));
    await user.type(screen.getByLabelText("Search conversations"), "billing");
    await user.selectOptions(screen.getByLabelText("Filter by bot"), "bot-001");
    await user.selectOptions(screen.getByLabelText("Export format"), "xlsx");
    await user.click(screen.getByRole("button", { name: "Export" }));

    expect(exportApi.downloadOperationalExport).toHaveBeenCalledWith(
      "conversations",
      "xlsx",
      expect.objectContaining({
        search: "billing",
        botId: "bot-001",
        contained: false,
      }),
    );

    await user.click(screen.getByText(/1:15/));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "More actions" }));
    expect(within(dialog).getByRole("menuitem", {
      name: "Export transcript as CSV",
    })).toBeInTheDocument();
    expect(within(dialog).getByRole("menuitem", {
      name: "Export transcript as Excel",
    })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("menuitem", {
      name: "Export transcript as CSV",
    }));
    expect(exportApi.downloadConversationTranscript)
      .toHaveBeenCalledWith("cv-001", "csv");
  });
});
