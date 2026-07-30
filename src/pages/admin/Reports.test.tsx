import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Reports from "@/pages/admin/Reports";
import * as api from "@/services/api";
import * as reportApi from "@/services/reportDownload";

const toast = vi.fn();

vi.mock("@/services/api", () => ({
  getPlatformAnalytics: vi.fn(),
}));
vi.mock("@/services/reportDownload", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/reportDownload")>();
  return { ...actual, downloadReport: vi.fn() };
});
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast }),
}));

const ANALYTICS = {
  labels: ["Jul 24"],
  callVol: [10],
  revenue: [100],
  aiCost: [12],
  callsSeries: [{ t: "Jul 24", calls: 10 }],
  revVsCost: [{ t: "Jul 24", revenue: 100, aiCost: 12 }],
  planMix: [],
  mrrByPlan: [{ label: "Growth", value: 3000 }],
  topTenantsByCalls: [{ label: "Tenant", value: 10 }],
  aiCostByProvider: [{ label: "LLM", value: 12 }],
};

describe("Super Admin Reports downloads", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getPlatformAnalytics).mockResolvedValue(ANALYTICS);
    vi.mocked(reportApi.downloadReport).mockImplementation(
      async (type, format) => `${type}.${format}`,
    );
  });

  it("exposes working CSV/Excel controls for every report tab", async () => {
    const user = userEvent.setup();
    render(<Reports />);
    await screen.findByText("Daily calls");

    await user.click(screen.getByRole("button", { name: "Download" }));
    expect(reportApi.downloadReport).toHaveBeenLastCalledWith(
      "usage", "csv", { days: 30 },
    );

    await user.click(screen.getByRole("tab", { name: "Revenue" }));
    await user.selectOptions(screen.getByLabelText("Export format"), "xlsx");
    await user.click(screen.getByRole("button", { name: "Download" }));
    expect(reportApi.downloadReport).toHaveBeenLastCalledWith(
      "revenue", "xlsx", { days: 30 },
    );

    await user.click(screen.getByRole("tab", { name: "AI Cost" }));
    await user.click(screen.getByRole("button", { name: "Download" }));
    expect(reportApi.downloadReport).toHaveBeenLastCalledWith(
      "ai_cost", "xlsx", { days: 30 },
    );
  });

  it("keeps the date filter functional and exports the selected range", async () => {
    const user = userEvent.setup();
    render(<Reports />);
    await screen.findByText("Daily calls");
    await user.click(screen.getByRole("button", { name: "90d" }));
    await waitFor(() => expect(api.getPlatformAnalytics).toHaveBeenLastCalledWith(90));
    await user.click(screen.getByRole("button", { name: "Download" }));
    expect(reportApi.downloadReport).toHaveBeenLastCalledWith(
      "usage", "csv", { days: 90 },
    );
  });
});
