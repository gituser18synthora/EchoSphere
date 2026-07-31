import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Conversations from "@/pages/tenant/Conversations";
import * as api from "@/services/api";
import * as exportApi from "@/services/exportDownload";

vi.mock("@/services/api", () => ({
  listConversations: vi.fn(),
  getConversation: vi.fn(),
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
  recording: null,
};

const DETAIL = {
  ...CONVERSATION,
  transcript: [
    { turn: 1, speaker: "bot", text: "Namaste, this is Billing Bot.", at: "2026-07-24T10:00:02.000Z", route: "workflow", latencyMs: 812 },
    { turn: 2, speaker: "user", text: "I was charged twice.", at: "2026-07-24T10:00:09.000Z" },
    { turn: 3, speaker: "bot", text: "Let me check that for you.", intent: "billing_dispute", confidence: 0.94, latencyMs: 640 },
  ],
  recording: null,
};

const RECORDING = {
  url: "/api/v1/conversations/cv-001/recording",
  mimeType: "audio/wav",
  durationSec: 74.5,
  sizeBytes: 2400000,
};

async function openDrawer(user: ReturnType<typeof userEvent.setup>) {
  render(<MemoryRouter><Conversations /></MemoryRouter>);
  await user.click(await screen.findByText(/1:15/));
  return screen.findByRole("dialog");
}

describe("Tenant conversation review", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listConversations).mockResolvedValue([CONVERSATION] as never);
    vi.mocked(api.getConversation).mockResolvedValue(DETAIL as never);
    vi.mocked(exportApi.downloadOperationalExport).mockResolvedValue("conversations.xlsx");
    vi.mocked(exportApi.downloadConversationTranscript).mockResolvedValue("transcript.csv");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
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
      expect.objectContaining({ search: "billing", botId: "bot-001", contained: false }),
    );

    await user.click(screen.getByText(/1:15/));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "More actions" }));
    expect(within(dialog).getByRole("menuitem", { name: "Export transcript as CSV" })).toBeInTheDocument();
    expect(within(dialog).getByRole("menuitem", { name: "Export transcript as Excel" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("menuitem", { name: "Export transcript as CSV" }));
    expect(exportApi.downloadConversationTranscript).toHaveBeenCalledWith("cv-001", "csv");
  });

  it("loads the transcript from the detail endpoint with speakers, order and timestamps", async () => {
    const user = userEvent.setup();
    const dialog = await openDrawer(user);

    expect(api.getConversation).toHaveBeenCalledWith("cv-001");
    expect(await within(dialog).findByText("Namaste, this is Billing Bot.")).toBeInTheDocument();
    expect(within(dialog).getByText("I was charged twice.")).toBeInTheDocument();
    expect(within(dialog).getByText("Let me check that for you.")).toBeInTheDocument();

    // Chronological order as rendered.
    const bubbles = [...dialog.querySelectorAll(".transcript-bubble")].map((b) => b.textContent);
    expect(bubbles).toEqual([
      "Namaste, this is Billing Bot.",
      "I was charged twice.",
      "Let me check that for you.",
    ]);
    // Speaker labels and per-turn detail.
    expect(within(dialog).getAllByText(/caller/).length).toBeGreaterThan(0);
    expect(within(dialog).getByText("billing_dispute", { selector: "code" })).toBeInTheDocument();
    expect(within(dialog).getByText(/640ms/)).toBeInTheDocument();
    // Timestamp rendered for turns that carry one (h:mm:ss in the meta line).
    const metas = [...dialog.querySelectorAll(".transcript-meta")].map((m) => m.textContent ?? "");
    expect(metas.filter((m) => /\d{1,2}:\d{2}:\d{2}/.test(m)).length).toBe(2);
  });

  it("shows an empty state when no turns were captured", async () => {
    vi.mocked(api.getConversation).mockResolvedValue({ ...DETAIL, transcript: [] } as never);
    const user = userEvent.setup();
    const dialog = await openDrawer(user);
    expect(await within(dialog).findByText("No transcript captured")).toBeInTheDocument();
  });

  it("surfaces a transcript loading error with retry", async () => {
    vi.mocked(api.getConversation).mockRejectedValueOnce(new Error("boom"));
    const user = userEvent.setup();
    const dialog = await openDrawer(user);
    expect(await within(dialog).findByText(/boom/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: /try again|retry/i }));
    expect(await within(dialog).findByText("Namaste, this is Billing Bot.")).toBeInTheDocument();
  });

  it("shows a graceful note instead of a player when there is no recording", async () => {
    const user = userEvent.setup();
    const dialog = await openDrawer(user);
    expect(await within(dialog).findByText(/No call recording is available/)).toBeInTheDocument();
    expect(dialog.querySelector("audio")).toBeNull();
  });

  it("fetches the recording with authorization and renders the player", async () => {
    localStorage.setItem("echosphere.token", "jwt-token");
    vi.mocked(api.getConversation).mockResolvedValue({ ...DETAIL, recording: RECORDING } as never);
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      headers: new Headers({ "content-type": "audio/wav" }),
      blob: async () => new Blob([new Uint8Array([82, 73, 70, 70])], { type: "audio/wav" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    URL.createObjectURL = vi.fn(() => "blob:recording");
    URL.revokeObjectURL = vi.fn();

    const user = userEvent.setup();
    const dialog = await openDrawer(user);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      RECORDING.url,
      expect.objectContaining({ headers: expect.objectContaining({ Authorization: "Bearer jwt-token" }) }),
    ));
    const audio = await waitFor(() => {
      const el = dialog.querySelector("audio");
      expect(el).not.toBeNull();
      return el as HTMLAudioElement;
    });
    expect(audio).toHaveAttribute("controls");
    expect(audio.getAttribute("src")).toBe("blob:recording");
    expect(within(dialog).getByRole("button", { name: "Download" })).toBeInTheDocument();
  });

  it("shows an actionable error when the recording file is gone", async () => {
    vi.mocked(api.getConversation).mockResolvedValue({ ...DETAIL, recording: RECORDING } as never);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      headers: new Headers({ "content-type": "application/json" }),
    }));
    const user = userEvent.setup();
    const dialog = await openDrawer(user);
    expect(await within(dialog).findByText(/no longer available/)).toBeInTheDocument();
    expect(dialog.querySelector("audio")).toBeNull();
    expect(within(dialog).getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});
