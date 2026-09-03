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
  useApp: () => ({ toast: vi.fn(), hasPermission: () => true }),
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
  // Backend-derived: 0.12 USD over 75s = 0.096 USD/min.
  costPerMinuteUsd: 0.096,
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
  characterUsage: {
    sttInputCharacters: 148,
    ttsOutputCharacters: 624,
    sttInputCharactersPerMin: 118.4,
    ttsOutputCharactersPerMin: 499.2,
  },
};

const RECORDING = {
  url: "/api/v1/conversations/cv-001/recording",
  mimeType: "audio/wav",
  durationSec: 74.5,
  sizeBytes: 2400000,
};

/* Mirrors the page's formatter so the expectations hold in whatever timezone
   the test machine is in — the assertion is "rendered in local time", not a
   hardcoded clock reading. */
const fmtDateTime = (d: Date) =>
  `${d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })}, ${
    d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" })}`;

async function openDrawer(user: ReturnType<typeof userEvent.setup>) {
  render(<MemoryRouter><Conversations /></MemoryRouter>);
  await user.click(await screen.findByText("1m 15s"));
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

  it("shows the call date and hides the QA score column", async () => {
    render(<MemoryRouter><Conversations /></MemoryRouter>);

    expect(await screen.findByText(/24 Jul 2026/)).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /QA score/i })).not.toBeInTheDocument();
  });

  it("shows per-call STT and TTS character rates in details", async () => {
    const user = userEvent.setup();
    const dialog = await openDrawer(user);

    expect(await within(dialog).findByText("Avg STT Input Characters / Min")).toBeInTheDocument();
    expect(within(dialog).getByText("118.4")).toBeInTheDocument();
    expect(within(dialog).getByText("148 input characters")).toBeInTheDocument();
    expect(within(dialog).getByText("Avg TTS Output Characters / Min")).toBeInTheDocument();
    expect(within(dialog).getByText("499.2")).toBeInTheDocument();
    expect(within(dialog).getByText("624 output characters")).toBeInTheDocument();
  });

  it("keeps the combined Date / Time & Duration column immediately before Cost", async () => {
    render(<MemoryRouter><Conversations /></MemoryRouter>);

    const headers = (await screen.findAllByRole("columnheader")).map((h) => h.textContent?.trim());
    // One combined call-timing column, directly before the cost column.
    const at = headers.indexOf("Date / Time & Duration");
    expect(at).toBeGreaterThan(-1);
    expect(headers[at + 1]).toMatch(/^Cost \(USD\)/);
    expect(headers.filter((h) => h === "Date / Time & Duration")).toHaveLength(1);

    // "24 Jul 2026, 03:30 PM" on the first line (viewer's timezone, from the
    // call's own startedAt), the labelled duration on the second — same cell.
    const started = new Date(CONVERSATION.startedAt);
    const dateTime = screen.getByText(fmtDateTime(started));
    const duration = screen.getByText("1m 15s");
    expect(dateTime.closest("td")).toBe(duration.closest("td"));
    expect(screen.getByText(/^Duration:/)).toBeInTheDocument();
    // The machine-readable value is the instant the API returned.
    expect(dateTime.closest("time")).toHaveAttribute("datetime", CONVERSATION.startedAt);
  });

  it("sorts by Date / Time & Duration both ways from the header", async () => {
    const later = {
      ...CONVERSATION,
      id: "cv-002",
      startedAt: "2026-07-25T10:00:00Z",
      durationSec: 35,
    };
    vi.mocked(api.listConversations).mockResolvedValue([CONVERSATION, later] as never);
    const user = userEvent.setup();
    render(<MemoryRouter><Conversations /></MemoryRouter>);
    await screen.findByText("1m 15s");

    const header = screen.getByRole("columnheader", { name: "Date / Time & Duration" });
    const firstRowId = () => screen.getAllByRole("row")[1].textContent;

    // The chevron's slot is reserved (hidden) before any sort, so the column
    // cannot widen and shift the table when sorting starts.
    const chevron = header.querySelector("svg")!;
    expect(chevron).not.toBeNull();
    expect(chevron.style.visibility).toBe("hidden");

    await user.click(header);
    expect(header).toHaveAttribute("aria-sort", "ascending");
    expect(firstRowId()).toContain("cv-001");
    expect(chevron.style.visibility).toBe("visible");

    await user.click(header);
    expect(header).toHaveAttribute("aria-sort", "descending");
    expect(firstRowId()).toContain("cv-002");
  });

  it.each([
    [35, "35 sec"],
    [0, "0 sec"],
    [134, "2m 14s"],
    [3932, "1h 05m 32s"],
  ])("renders a %i second call as %s", async (durationSec, expected) => {
    vi.mocked(api.listConversations).mockResolvedValue(
      [{ ...CONVERSATION, durationSec }] as never,
    );
    render(<MemoryRouter><Conversations /></MemoryRouter>);
    expect(await screen.findByText(expected)).toBeInTheDocument();
  });

  /** Walk the picker's calendar back to the month with the given title. */
  async function gotoMonth(user: ReturnType<typeof userEvent.setup>, title: string) {
    const dialog = await screen.findByRole("dialog", { name: "Choose date range" });
    for (let hops = 0; hops < 48 && !within(dialog).queryByText(title); hops++) {
      await user.click(within(dialog).getByRole("button", { name: "Previous month" }));
    }
    expect(within(dialog).getByText(title)).toBeInTheDocument();
    return dialog;
  }

  it("filters by date range through the API, in the viewer's timezone", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><Conversations /></MemoryRouter>);
    await screen.findByText("1m 15s");

    await user.click(screen.getByRole("button", { name: "Filter by date range" }));
    const dialog = await gotoMonth(user, "July 2026");
    await user.click(within(dialog).getByRole("button", { name: "20 July 2026" }));
    await user.click(within(dialog).getByRole("button", { name: "24 July 2026" }));

    // The picked local days are sent as instants, so the range selects exactly
    // the calls the page displays under those dates.
    await waitFor(() => expect(api.listConversations).toHaveBeenLastCalledWith({
      dateFrom: new Date(2026, 6, 20, 0, 0, 0, 0).toISOString(),
      dateTo: new Date(2026, 6, 24, 23, 59, 59, 999).toISOString(),
    }));
    // The trigger echoes the committed range and the popover is gone.
    expect(screen.getByRole("button", { name: "Filter by date range" })).toHaveTextContent("20 Jul – 24 Jul 2026");
    expect(screen.queryByRole("dialog", { name: "Choose date range" })).not.toBeInTheDocument();

    // The same window travels with the export.
    await user.click(screen.getByRole("button", { name: "Export" }));
    expect(exportApi.downloadOperationalExport).toHaveBeenCalledWith(
      "conversations",
      expect.any(String),
      expect.objectContaining({
        dateFrom: new Date(2026, 6, 20, 0, 0, 0, 0).toISOString(),
        dateTo: new Date(2026, 6, 24, 23, 59, 59, 999).toISOString(),
      }),
    );

    // Clearing goes back to an unbounded window.
    await user.click(screen.getByRole("button", { name: "Filter by date range" }));
    await user.click(await screen.findByRole("button", { name: "Clear dates" }));
    await waitFor(() => expect(api.listConversations).toHaveBeenLastCalledWith({
      dateFrom: undefined,
      dateTo: undefined,
    }));
    expect(screen.getByRole("button", { name: "Filter by date range" })).toHaveTextContent("All dates");
  });

  it("supports single-day ranges and never sends an inverted one", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><Conversations /></MemoryRouter>);
    await screen.findByText("1m 15s");

    // Clicking the same day twice = that whole day.
    await user.click(screen.getByRole("button", { name: "Filter by date range" }));
    let dialog = await gotoMonth(user, "July 2026");
    await user.click(within(dialog).getByRole("button", { name: "24 July 2026" }));
    await user.click(within(dialog).getByRole("button", { name: "24 July 2026" }));
    await waitFor(() => expect(api.listConversations).toHaveBeenLastCalledWith({
      dateFrom: new Date(2026, 6, 24, 0, 0, 0, 0).toISOString(),
      dateTo: new Date(2026, 6, 24, 23, 59, 59, 999).toISOString(),
    }));

    // Picking the earlier day second swaps the ends instead of asking the API
    // for a range it would reject with a 422 (blanking the table).
    await user.click(screen.getByRole("button", { name: "Filter by date range" }));
    dialog = await screen.findByRole("dialog", { name: "Choose date range" });
    await user.click(within(dialog).getByRole("button", { name: "24 July 2026" }));
    await user.click(within(dialog).getByRole("button", { name: "20 July 2026" }));
    await waitFor(() => expect(api.listConversations).toHaveBeenLastCalledWith(
      expect.objectContaining({ dateFrom: new Date(2026, 6, 20, 0, 0, 0, 0).toISOString() }),
    ));

    for (const call of vi.mocked(api.listConversations).mock.calls) {
      const { dateFrom: start, dateTo: end } = call[0] ?? {};
      if (start && end) expect(new Date(start).getTime()).toBeLessThan(new Date(end).getTime());
    }
    expect(screen.getByRole("table")).toBeInTheDocument();
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

    await user.click(screen.getByText("1m 15s"));
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
    const bubbles = [...dialog.querySelectorAll(".transcript-text")].map((b) => b.textContent);
    expect(bubbles).toEqual([
      "Namaste, this is Billing Bot.",
      "I was charged twice.",
      "Let me check that for you.",
    ]);
    // Speaker alignment and per-turn detail.
    expect(dialog.querySelectorAll(".conversation-turn.bot")).toHaveLength(2);
    expect(dialog.querySelectorAll(".conversation-turn.user")).toHaveLength(1);
    expect(within(dialog).getByText("billing_dispute", { selector: "code" })).toBeInTheDocument();
    expect(within(dialog).getByText(/640ms/)).toBeInTheDocument();
    // Stored transcript uses the same in-bubble MM:SS.xx clock as Testing.
    const times = [...dialog.querySelectorAll(".transcript-bubble time")];
    expect(times.map((time) => time.textContent)).toEqual([
      expect.stringMatching(/^\d{2}:\d{2}\.\d{2}$/),
      expect.stringMatching(/^\d{2}:\d{2}\.\d{2}$/),
    ]);
    expect(times.every((time) => time.closest(".transcript-bubble"))).toBe(true);
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

  it("stacks the metered total and per-minute rate in one cost column", async () => {
    render(<MemoryRouter><Conversations /></MemoryRouter>);
    expect(await screen.findByRole("columnheader", { name: /Cost \(USD\)/ })).toBeInTheDocument();
    // Total on the first line, the backend-derived rate on the second — both
    // rendered as given, in the same cell.
    const total = screen.getByText("$0.12");
    const perMin = screen.getByText("$0.096");
    expect(total.closest("td")).toBe(perMin.closest("td"));
    expect(screen.getByText(/^Total:/)).toBeInTheDocument();
    expect(screen.getByText(/^Per min:/)).toBeInTheDocument();
  });

  it("shows a dash, not a rate, for a call that never connected", async () => {
    vi.mocked(api.listConversations).mockResolvedValue(
      [{ ...CONVERSATION, durationSec: 0, costUsd: 0, costPerMinuteUsd: null }] as never,
    );
    render(<MemoryRouter><Conversations /></MemoryRouter>);
    await screen.findByText("0 sec");
    expect(screen.getByText(/^Per min:/)).toHaveTextContent("Per min: —");
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

describe("AI call summary display", () => {
  const SUMMARY = {
    status: "completed",
    callOutcome: "promise_to_pay",
    summary: "Customer cannot pay today and committed to paying ₹2,000 on Monday.",
    customerIntent: "delay_payment",
    customerSentiment: "cooperative",
    customerCommitments: [
      { type: "payment", description: "pay two thousand on Monday", amount: 2000, currency: "INR", dueDate: "2026-08-10", status: "promised" },
    ],
    objections: [],
    importantFacts: ["Customer gets salary on the 10th"],
    resolvedItems: ["identity_confirmation"],
    unresolvedItems: ["remaining_balance"],
    missingSlots: ["payment_method"],
    nextBestAction: { action: "follow_up_on_commitment", reason: "Open commitment", priority: "medium", recommendedAt: "2026-08-10T04:30:00Z" },
    followUpRequired: true,
    followUpAt: "2026-08-10T04:30:00Z",
    confidence: 0.92,
    generatedAt: "2026-08-07T12:00:00Z",
    error: null,
  };

  beforeEach(() => {
    vi.mocked(api.listConversations).mockResolvedValue([CONVERSATION] as never);
  });

  it("renders summary, outcome, next action, commitments and pending items", async () => {
    vi.mocked(api.getConversation).mockResolvedValue({ ...DETAIL, summary: SUMMARY } as never);
    const user = userEvent.setup();
    const dialog = await openDrawer(user);
    expect(await within(dialog).findByText("AI call summary")).toBeInTheDocument();
    expect(within(dialog).getByText(/committed to paying ₹2,000 on Monday/)).toBeInTheDocument();
    expect(within(dialog).getByText("promise to pay")).toBeInTheDocument();
    expect(within(dialog).getByText("follow up on commitment")).toBeInTheDocument();
    expect(within(dialog).getByText(/pay two thousand on Monday/)).toBeInTheDocument();
    expect(within(dialog).getByText("remaining balance")).toBeInTheDocument();
    expect(within(dialog).getByText("Follow-up")).toBeInTheDocument();
  });

  it("renders the configured structured summary fields with their values", async () => {
    vi.mocked(api.getConversation).mockResolvedValue({
      ...DETAIL,
      summary: {
        ...SUMMARY,
        structuredFields: {
          call_customer: "Yes",
          reach_customer_location: "Yes",
          hand_over_product: "Yes",
          hand_over_to: "security_guard",
          call_cx: null,
        },
        structuredFieldSources: { call_customer: "workflow", hand_over_to: "workflow" },
      },
    } as never);
    const user = userEvent.setup();
    const dialog = await openDrawer(user);
    const section = await within(dialog).findByTestId("structured-summary");
    expect(within(section).getByText("Structured summary")).toBeInTheDocument();
    expect(within(section).getByText("hand over to")).toBeInTheDocument();
    expect(within(section).getByText("security guard")).toBeInTheDocument();
    expect(within(section).getAllByText("Yes")).toHaveLength(3);
    expect(within(section).getByText("not determined")).toBeInTheDocument();
  });

  it("shows a processing state while the analysis is pending", async () => {
    vi.mocked(api.getConversation).mockResolvedValue({
      ...DETAIL,
      summary: { ...SUMMARY, status: "processing", summary: null, callOutcome: null, nextBestAction: null, customerCommitments: [], importantFacts: [], unresolvedItems: [], missingSlots: [] },
    } as never);
    const user = userEvent.setup();
    const dialog = await openDrawer(user);
    expect(await within(dialog).findByText(/still being generated/)).toBeInTheDocument();
  });

  it("shows the failure state with the deterministic fallback", async () => {
    vi.mocked(api.getConversation).mockResolvedValue({
      ...DETAIL,
      summary: { ...SUMMARY, status: "failed", summary: null, error: "analysis_unavailable" },
    } as never);
    const user = userEvent.setup();
    const dialog = await openDrawer(user);
    expect(await within(dialog).findByText(/could not be generated/)).toBeInTheDocument();
    expect(within(dialog).getByText(/deterministic fallback/)).toBeInTheDocument();
  });

  it("renders nothing when no analysis exists for the call", async () => {
    vi.mocked(api.getConversation).mockResolvedValue({ ...DETAIL, summary: null } as never);
    const user = userEvent.setup();
    const dialog = await openDrawer(user);
    await within(dialog).findByText(/Namaste, this is Billing Bot/);
    expect(within(dialog).queryByText("AI call summary")).not.toBeInTheDocument();
  });
});
