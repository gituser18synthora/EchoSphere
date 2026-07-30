import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Subscriptions from "@/pages/admin/Subscriptions";
import * as api from "@/services/api";
import * as exportApi from "@/services/exportDownload";

vi.mock("@/services/api", () => ({
  listSubscriptions: vi.fn(),
}));
vi.mock("@/services/exportDownload", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/exportDownload")>();
  return { ...actual, downloadOperationalExport: vi.fn() };
});
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn() }),
}));

const SUBSCRIPTIONS = [
  {
    id: "sub-001",
    tenantId: "tn-001",
    tenant: "Acme Health",
    plan: "growth",
    seats: 10,
    botLimit: 4,
    minutesIncluded: 10000,
    minutesUsed: 2500,
    renewsAt: "2026-08-01",
    status: "active",
    mrr: 500,
  },
  {
    id: "sub-002",
    tenantId: "tn-002",
    tenant: "Northwind",
    plan: "starter",
    seats: 5,
    botLimit: 2,
    minutesIncluded: 5000,
    minutesUsed: 1000,
    renewsAt: "2026-08-15",
    status: "trial",
    mrr: 100,
  },
] as const;

describe("Super Admin Subscriptions export", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listSubscriptions).mockResolvedValue(
      SUBSCRIPTIONS as never,
    );
    vi.mocked(exportApi.downloadOperationalExport)
      .mockResolvedValue("subscriptions.xlsx");
  });

  it("offers CSV/Excel and sends the current search, status and plan filters", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><Subscriptions /></MemoryRouter>);
    await screen.findByText("Acme Health");

    await user.type(screen.getByLabelText("Search subscriptions"), "Acme");
    await user.selectOptions(
      screen.getByLabelText("Filter subscriptions by status"),
      "active",
    );
    await user.selectOptions(
      screen.getByLabelText("Filter subscriptions by plan"),
      "growth",
    );
    expect(screen.queryByText("Northwind")).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Export format"), "xlsx");
    await user.click(screen.getByRole("button", { name: "Export" }));

    expect(exportApi.downloadOperationalExport).toHaveBeenCalledWith(
      "subscriptions",
      "xlsx",
      { search: "Acme", status: "active", plan: "growth" },
    );
  });
});
