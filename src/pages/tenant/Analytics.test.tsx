import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Analytics from "@/pages/tenant/Analytics";
import * as api from "@/services/api";
import * as reportApi from "@/services/reportDownload";

vi.mock("@/services/api", () => ({
  getTenantAnalytics: vi.fn(),
  getUsageSummary: vi.fn(),
  getCurrencyRates: vi.fn(),
}));
vi.mock("@/services/reportDownload", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/reportDownload")>();
  return { ...actual, downloadReport: vi.fn() };
});
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({
    user: { tenantName: "Meridian Health", tenantId: "tn-001" },
    toast: vi.fn(),
    hasPermission: () => true, // tenant admin view — costs visible
  }),
}));

const TENANT_ANALYTICS = {
  kpis: [],
  callsSeries: [{ t: "Jul 24", calls: 5, contained: 4 }],
  containmentSeries: [{ t: "Jul 24", rate: 80 }],
  sentimentSplit: [{ label: "Neutral", value: 100 }],
  languageMix: [{ label: "English", value: 100 }],
  topIntents: [],
  knowledgeUsage: [],
  costSeries: [{ t: "Jul 24", llm: 1, tts: 1, stt: 1, telephony: 1 }],
  recommendations: [],
};

const EMPTY_QUANTITIES = {
  requests: 0, inputTokens: 0, outputTokens: 0, cachedTokens: 0, totalTokens: 0,
  characters: 0, audioSeconds: 0, costUsd: 0, missingPriceEvents: 0,
};

const USAGE_SUMMARY = {
  tenantId: "tn-001",
  period: { start: "2026-07-01T00:00:00Z", end: "2026-07-24T00:00:00Z", days: 30 },
  baseCurrency: "USD",
  totalCostUsd: 12.45,
  totalCostConverted: { INR: 1076.93 },
  missingPriceEvents: 1,
  capabilities: {
    llm: { ...EMPTY_QUANTITIES, requests: 4, inputTokens: 1000, outputTokens: 500, totalTokens: 1500, costUsd: 10 },
    embedding: { ...EMPTY_QUANTITIES, totalTokens: 800, costUsd: 0.02 },
    stt: { ...EMPTY_QUANTITIES, audioSeconds: 120, missingPriceEvents: 1 },
    tts: { ...EMPTY_QUANTITIES, characters: 640, costUsd: 2.43 },
    telephony: { ...EMPTY_QUANTITIES },
  },
  byProviderModel: [
    { capability: "llm", provider: "openai", model: "gpt-4o-mini", ...EMPTY_QUANTITIES, requests: 4, totalTokens: 1500, costUsd: 10 },
    { capability: "stt", provider: "sarvam", model: "saaras:v3", ...EMPTY_QUANTITIES, requests: 1, audioSeconds: 120, missingPriceEvents: 1 },
  ],
};

const CURRENCY_RATES = {
  baseCurrency: "USD",
  currencies: [
    { code: "USD", name: "US Dollar", symbol: "$", decimalPlaces: 2, isBase: true, hasRate: true },
    { code: "INR", name: "Indian Rupee", symbol: "₹", decimalPlaces: 2, isBase: false, hasRate: true },
    { code: "GBP", name: "British Pound", symbol: "£", decimalPlaces: 2, isBase: false, hasRate: false },
  ],
  rates: { INR: 86.5 },
};

describe("Tenant Analytics downloads", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getTenantAnalytics).mockResolvedValue(TENANT_ANALYTICS as never);
    vi.mocked(api.getUsageSummary).mockResolvedValue(USAGE_SUMMARY as never);
    vi.mocked(api.getCurrencyRates).mockResolvedValue(CURRENCY_RATES as never);
    vi.mocked(reportApi.downloadReport).mockResolvedValue("tenant-report.csv");
  });

  it("lets the tenant export Usage or AI Cost as CSV or Excel", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><Analytics /></MemoryRouter>);
    await screen.findByText("Calls & containment");

    await user.selectOptions(screen.getByLabelText("Report type"), "ai_cost");
    await user.selectOptions(screen.getByLabelText("Export format"), "xlsx");
    await user.click(screen.getByRole("button", { name: "Download" }));

    expect(reportApi.downloadReport).toHaveBeenCalledWith(
      "ai_cost", "xlsx", { days: 30 },
    );
  });
});

describe("Tenant Analytics usage & cost", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getTenantAnalytics).mockResolvedValue(TENANT_ANALYTICS as never);
    vi.mocked(api.getUsageSummary).mockResolvedValue(USAGE_SUMMARY as never);
    vi.mocked(api.getCurrencyRates).mockResolvedValue(CURRENCY_RATES as never);
    vi.mocked(reportApi.downloadReport).mockResolvedValue("tenant-report.csv");
  });

  it("shows per-capability usage with USD totals and a missing-pricing note", async () => {
    render(<MemoryRouter><Analytics /></MemoryRouter>);
    await screen.findByText("AI & API usage");

    // Appears in the capability card and again in the provider drill-down.
    expect(screen.getAllByText("1,500 tokens").length).toBeGreaterThan(0);
    expect(screen.getByText("640 chars")).toBeInTheDocument();
    // USD-only display until another currency is selected.
    expect(screen.getByText("$12.45")).toBeInTheDocument();
    expect(screen.getByText(/Pricing unavailable for 1 event/)).toBeInTheDocument();
    // Unpriced provider rows say so instead of showing a fabricated zero.
    expect(screen.getByText("Pricing unavailable")).toBeInTheDocument();
  });

  it("switches to dual USD/INR display without changing the base cost", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><Analytics /></MemoryRouter>);
    await screen.findByText("AI & API usage");

    await user.selectOptions(screen.getByLabelText("Display currency"), "INR");
    expect(await screen.findByText("$12.45 / ₹1,076.93")).toBeInTheDocument();
    // The stored base amount is still USD — only the rendering changed.
    expect(vi.mocked(api.getUsageSummary)).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem("echosphere.displayCurrency")).toBe("INR");
  });

  it("disables display currencies that have no configured rate", async () => {
    render(<MemoryRouter><Analytics /></MemoryRouter>);
    await screen.findByText("AI & API usage");
    const gbp = screen.getByRole("option", { name: /GBP.*no rate/ }) as HTMLOptionElement;
    expect(gbp.disabled).toBe(true);
  });
});
