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
  // Cost rendering goes through the shared display-currency hook, which loads
  // the backend's rate table.
  getCurrencyRates: vi.fn(),
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

const CURRENCY_RATES = {
  baseCurrency: "USD",
  currencies: [
    { code: "USD", name: "US Dollar", symbol: "$", decimalPlaces: 2, isBase: true, hasRate: true },
    { code: "INR", name: "Indian Rupee", symbol: "₹", decimalPlaces: 2, isBase: false, hasRate: true },
  ],
  rates: { INR: 96.5 },
};

/* The breakdown the detail endpoint returns: rebuilt server-side from the
   call's usage events and the rate snapshot recorded at the time. */
const COST = {
  sessionId: "vs_test",
  baseCurrency: "USD",
  totalUsd: "0.120000",
  displayCurrency: "USD",
  displayTotal: "0.120000",
  displayRate: null,
  byCapability: {
    tts: { label: "Text to speech", costUsd: "0.100000" },
    llm: { label: "Language model", costUsd: "0.020000" },
  },
  lines: [
    {
      capability: "tts", capabilityLabel: "Text to speech", provider: "elevenlabs",
      model: "eleven_flash_v2_5", voice: null, component: "characters",
      componentLabel: "Characters", quantity: "2000", unit: "per_1k_characters",
      unitPrice: "0.05", rateCurrency: "USD", fxRate: null, costUsd: "0.100000",
      priced: true, note: null,
    },
    {
      capability: "telephony", capabilityLabel: "Telephony", provider: "freeswitch",
      model: "", voice: null, component: "call_seconds", componentLabel: "Call time",
      quantity: "75", unit: "", unitPrice: "0", rateCurrency: "USD", fxRate: null,
      costUsd: "0", priced: false,
      note: "No active price configured — usage recorded but not costed.",
    },
  ],
  unpriced: ["telephony:freeswitch:call_seconds"],
  eventCount: 3,
  highCost: false,
  highCostThresholdUsd: "0.5",
  storedTotalUsd: "0.120000",
  reconciled: true,
};

describe("Tenant conversation review", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listConversations).mockResolvedValue([CONVERSATION] as never);
    vi.mocked(api.getConversation).mockResolvedValue({ ...DETAIL, cost: COST } as never);
    vi.mocked(api.getCurrencyRates).mockResolvedValue(CURRENCY_RATES as never);
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
    // MenuButton portals its popup to document.body so it cannot be clipped by
    // the drawer's overflow boundary; query the popup at screen scope.
    expect(screen.getByRole("menuitem", { name: "Export transcript as CSV" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Export transcript as Excel" })).toBeInTheDocument();
    await user.click(screen.getByRole("menuitem", { name: "Export transcript as CSV" }));
    expect(exportApi.downloadConversationTranscript).toHaveBeenCalledWith("cv-001", "csv");
  });

  it("loads the transcript from the detail endpoint with speakers, order and timestamps", async () => {
    const user = userEvent.setup();
    const dialog = await openDrawer(user);

    // The display currency is passed so the breakdown comes back converted.
    expect(api.getConversation).toHaveBeenCalledWith("cv-001", "USD");
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

describe("Conversation costing display", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listConversations).mockResolvedValue([CONVERSATION] as never);
    vi.mocked(api.getConversation).mockResolvedValue({ ...DETAIL, cost: COST } as never);
    vi.mocked(api.getCurrencyRates).mockResolvedValue(CURRENCY_RATES as never);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("shows the metered cost in the list, headed with the display currency", async () => {
    render(<MemoryRouter><Conversations /></MemoryRouter>);
    expect(await screen.findByRole("columnheader", { name: /Cost \(USD\)/ })).toBeInTheDocument();
    expect(screen.getByText("$0.12")).toBeInTheDocument();
  });

  it("renders the list, recording row and breakdown from ONE backend total", async () => {
    const user = userEvent.setup();
    const dialog = await openDrawer(user);
    // 0.12 formatted identically wherever it appears — the client never
    // recomputes it, so the three places cannot disagree.
    const shown = await within(dialog).findAllByText("$0.12");
    expect(shown.length).toBeGreaterThan(1);
    expect(within(dialog).getByText("Cost breakdown")).toBeInTheDocument();
  });

  it("converts to the selected currency using the backend rate", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><Conversations /></MemoryRouter>);
    await screen.findByText("$0.12");
    await user.selectOptions(await screen.findByLabelText("Display currency"), "INR");

    // 0.12 USD × 96.5 = ₹11.58, and the column header follows the selection.
    expect(await screen.findByRole("columnheader", { name: /Cost \(INR\)/ })).toBeInTheDocument();
    expect(screen.getByText("₹11.58")).toBeInTheDocument();
    expect(screen.queryByText("$0.12")).not.toBeInTheDocument();
  });

  it("re-requests the breakdown when the display currency changes", async () => {
    const user = userEvent.setup();
    const dialog = await openDrawer(user);
    await within(dialog).findByText("Cost breakdown");
    await user.selectOptions(screen.getByLabelText("Display currency"), "INR");
    await waitFor(() =>
      expect(api.getConversation).toHaveBeenCalledWith("cv-001", "INR"),
    );
  });

  it("itemises components, rates and unpriced usage on demand", async () => {
    const user = userEvent.setup();
    const dialog = await openDrawer(user);
    await user.click(await within(dialog).findByRole("button", { name: "Details" }));

    expect(within(dialog).getByText("elevenlabs / eleven_flash_v2_5")).toBeInTheDocument();
    expect(within(dialog).getByText(/per 1k characters/)).toBeInTheDocument();
    expect(within(dialog).getByText("2,000")).toBeInTheDocument();
    // An unpriced component is shown with its reason, never hidden.
    expect(within(dialog).getByText(/No active price configured/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Not costed \(no configured price\)/)).toBeInTheDocument();
    // Rounding is stated rather than left for the reader to infer.
    expect(within(dialog).getByText(/4 decimal places/)).toBeInTheDocument();
  });

  it("flags an unusually expensive call instead of rendering it as normal", async () => {
    vi.mocked(api.listConversations).mockResolvedValue(
      [{ ...CONVERSATION, costUsd: 0.9 }] as never,
    );
    vi.mocked(api.getConversation).mockResolvedValue({
      ...DETAIL, costUsd: 0.9,
      cost: { ...COST, totalUsd: "0.900000", storedTotalUsd: "0.900000", highCost: true },
    } as never);
    const user = userEvent.setup();
    const dialog = await openDrawer(user);
    expect(await within(dialog).findByText(/Unusually high/)).toBeInTheDocument();
  });

  it("warns when the stored total and the recomputed sum disagree", async () => {
    vi.mocked(api.getConversation).mockResolvedValue({
      ...DETAIL,
      cost: { ...COST, storedTotalUsd: "0.050000", reconciled: false },
    } as never);
    const user = userEvent.setup();
    const dialog = await openDrawer(user);
    await user.click(await within(dialog).findByRole("button", { name: "Details" }));
    expect(within(dialog).getByText(/Stored total differs/)).toBeInTheDocument();
  });

  it("degrades quietly when a call has no metered usage", async () => {
    vi.mocked(api.getConversation).mockResolvedValue({
      ...DETAIL, costUsd: 0,
      cost: { ...COST, totalUsd: "0", byCapability: {}, lines: [], eventCount: 0 },
    } as never);
    const user = userEvent.setup();
    const dialog = await openDrawer(user);
    expect(await within(dialog).findByText(/No metered usage recorded/)).toBeInTheDocument();
  });
});
