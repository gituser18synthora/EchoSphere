import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import VoicePlatform, { botMatches, phoneNumberMatches } from "@/pages/admin/VoicePlatform";
import * as api from "@/services/api";
import type { PhoneNumber, VoiceBot } from "@/types/domain";

vi.mock("@/services/api", () => ({
  listAllBots: vi.fn(),
  listTenants: vi.fn(),
  listPhoneNumbers: vi.fn(),
  listSipTrunks: vi.fn(() => Promise.resolve([])),
  listChannelsSummary: vi.fn(() => Promise.resolve([])),
  setPhoneNumberActive: vi.fn(),
  updatePhoneNumber: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), hasPermission: () => true }),
}));

const bot = (id: string, name: string): VoiceBot => ({
  id, name, tenantId: "tn_1", useCase: "", description: "", languages: ["hi-IN"],
  status: "published", version: "v1", owner: "", health: "good", containment: 0,
  callsMonth: 0,
} as unknown as VoiceBot);

const BOTS = [
  bot("bot_59a84478f155", "Zepto MDND Support"),
  bot("bot_4b8d6fe95cb1", "Zepto OB Verification"),
  bot("bot_ac634648c152", "AU Bank Debit Card"),
];

const number = (id: string, value: string, extra: Partial<PhoneNumber> = {}): PhoneNumber => ({
  id, number: value, country: "IN", provider: "FreeSWITCH", status: "available",
  isActive: true, monthlyCost: 0, ...extra,
});

const NUMBERS = [
  number("pn_1", "+918047133650", { tenant: "Zepto", bot: "Zepto MDND Support", status: "assigned" }),
  number("pn_2", "+918080813352"),
  number("pn_3", "+14155550119", { country: "US", provider: "Twilio" }),
];

function renderPage() {
  return render(<MemoryRouter><VoicePlatform /></MemoryRouter>);
}

describe("botMatches", () => {
  it("matches a partial bot name or bot ID, ignoring case and padding", () => {
    expect(botMatches(BOTS[0], "mdnd")).toBe(true);
    expect(botMatches(BOTS[0], "  ZEPTO mdnd ")).toBe(true);
    expect(botMatches(BOTS[0], "BOT_59A8")).toBe(true);
    expect(botMatches(BOTS[0], "4478f1")).toBe(true);
    expect(botMatches(BOTS[0], "debit")).toBe(false);
    expect(botMatches(BOTS[0], "")).toBe(true);
  });
});

describe("phoneNumberMatches", () => {
  const [mdnd, mumbai, us] = NUMBERS;

  it("matches any part of the number, ignoring spaces, dashes and brackets", () => {
    expect(phoneNumberMatches(mdnd, "8047133")).toBe(true);
    expect(phoneNumberMatches(mdnd, "80 4713-3650")).toBe(true);
    expect(phoneNumberMatches(mdnd, "(804) 713")).toBe(true);
    expect(phoneNumberMatches(mumbai, "8047133")).toBe(false);
  });

  it("a leading + or 00 matches the country code at the start", () => {
    expect(phoneNumberMatches(mdnd, "+91")).toBe(true);
    expect(phoneNumberMatches(mdnd, "+91 80471")).toBe(true);
    expect(phoneNumberMatches(mdnd, "0091 8047")).toBe(true);
    expect(phoneNumberMatches(us, "+91")).toBe(false);
    expect(phoneNumberMatches(us, "+1 415")).toBe(true);
    // "1" alone is a substring match, not a country code.
    expect(phoneNumberMatches(mdnd, "+1")).toBe(false);
  });

  it("tolerates a national trunk 0", () => {
    expect(phoneNumberMatches(mdnd, "080 4713 3650")).toBe(true);
    expect(phoneNumberMatches(us, "080 4713 3650")).toBe(false);
  });

  it("two letters are a country code; longer text matches tenant, bot or carrier", () => {
    expect(phoneNumberMatches(us, "us")).toBe(true);
    expect(phoneNumberMatches(mdnd, "IN")).toBe(true);
    expect(phoneNumberMatches(us, "IN")).toBe(false);
    // "in" inside a name is not the country IN.
    const finance = number("pn_4", "+447700900123", { country: "GB", tenant: "Manappuram Finance" });
    expect(phoneNumberMatches(finance, "in")).toBe(false);
    expect(phoneNumberMatches(finance, "finance")).toBe(true);
    expect(phoneNumberMatches(mdnd, "zepto")).toBe(true);
    expect(phoneNumberMatches(us, "twilio")).toBe(true);
    expect(phoneNumberMatches(mumbai, "zepto")).toBe(false);
  });
});

describe("VoicePlatform — Voice Bots search", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listAllBots).mockResolvedValue(BOTS);
    vi.mocked(api.listTenants).mockResolvedValue([{ id: "tn_1", name: "Zepto" }] as never);
  });

  it("filters by name or ID as the user types and shows the bot ID", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("AU Bank Debit Card");
    expect(screen.getByText("bot_59a84478f155")).toBeInTheDocument();

    const search = screen.getByRole("searchbox", { name: "Search voice bots" });
    await user.type(search, "zepto");
    expect(screen.queryByText("AU Bank Debit Card")).not.toBeInTheDocument();
    expect(screen.getByText("Zepto MDND Support")).toBeInTheDocument();
    expect(screen.getByText("Zepto OB Verification")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("2 of 3 bots");

    await user.clear(search);
    await user.type(search, "BOT_AC63");
    expect(screen.getByText("AU Bank Debit Card")).toBeInTheDocument();
    expect(screen.queryByText("Zepto MDND Support")).not.toBeInTheDocument();
    // Every keystroke filters the list already loaded — no refetch.
    expect(api.listAllBots).toHaveBeenCalledTimes(1);
  });

  it("shows a clear empty state that resets the search", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("AU Bank Debit Card");
    const search = screen.getByRole("searchbox", { name: "Search voice bots" });
    await user.type(search, "nothing-like-this");
    expect(screen.getByText("No bots match “nothing-like-this”")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear search" }));
    expect(search).toHaveValue("");
    expect(screen.getByText("AU Bank Debit Card")).toBeInTheDocument();
  });

  it("Escape clears the search", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("AU Bank Debit Card");
    const search = screen.getByRole("searchbox", { name: "Search voice bots" });
    await user.type(search, "mdnd{Escape}");
    expect(search).toHaveValue("");
    expect(screen.getAllByRole("row")).toHaveLength(BOTS.length + 1);
  });
});

describe("VoicePlatform — Phone Numbers search", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listAllBots).mockResolvedValue([]);
    vi.mocked(api.listTenants).mockResolvedValue([] as never);
    vi.mocked(api.listPhoneNumbers).mockResolvedValue(NUMBERS);
    vi.mocked(api.setPhoneNumberActive).mockResolvedValue(NUMBERS[1]);
  });

  async function openNumbers(user: ReturnType<typeof userEvent.setup>) {
    renderPage();
    await user.click(screen.getByRole("tab", { name: "Phone Numbers" }));
    await screen.findByText("+14155550119");
  }

  it("filters by partial number and country code without new API requests", async () => {
    const user = userEvent.setup();
    await openNumbers(user);
    const search = screen.getByRole("searchbox", { name: "Search phone numbers" });

    await user.type(search, "+91");
    expect(screen.queryByText("+14155550119")).not.toBeInTheDocument();
    expect(screen.getByText("+918047133650")).toBeInTheDocument();
    expect(screen.getByText("+918080813352")).toBeInTheDocument();

    await user.type(search, " 80 4713");
    expect(screen.getByText("+918047133650")).toBeInTheDocument();
    expect(screen.queryByText("+918080813352")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("1 of 3 numbers");
    expect(api.listPhoneNumbers).toHaveBeenCalledTimes(1);
  });

  it("shows a clear empty state for an unknown number", async () => {
    const user = userEvent.setup();
    await openNumbers(user);
    await user.type(screen.getByRole("searchbox", { name: "Search phone numbers" }), "+44");
    expect(screen.getByText("No numbers match “+44”")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear search" }));
    expect(screen.getByText("+14155550119")).toBeInTheDocument();
  });

  it("keeps the search across a reload after a row action", async () => {
    const user = userEvent.setup();
    await openNumbers(user);
    await user.type(screen.getByRole("searchbox", { name: "Search phone numbers" }), "8080");
    const row = screen.getByText("+918080813352").closest("tr")!;
    await user.click(within(row).getByRole("button", { name: "Deactivate" }));
    const confirm = await screen.findByRole("dialog");
    await user.click(within(confirm).getByRole("button", { name: "Deactivate" }));
    await waitFor(() => expect(api.listPhoneNumbers).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("+918080813352")).toBeInTheDocument();
    expect(screen.queryByText("+918047133650")).not.toBeInTheDocument();
  });
});
