import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Billing from "@/pages/admin/Billing";
import * as api from "@/services/api";
import * as exportApi from "@/services/exportDownload";

vi.mock("@/services/api", () => ({
  listInvoices: vi.fn(),
  listTenants: vi.fn(),
}));
vi.mock("@/services/exportDownload", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/exportDownload")>();
  return {
    ...actual,
    downloadOperationalExport: vi.fn(),
    downloadInvoicePdf: vi.fn(),
  };
});
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn() }),
}));

const INVOICES = [
  {
    id: "inv-001",
    tenantId: "tn-001",
    tenant: "Acme Health",
    period: "Jul 2026",
    amount: 500,
    status: "paid",
    issuedAt: "2026-07-01",
  },
  {
    id: "inv-002",
    tenantId: "tn-002",
    tenant: "Northwind",
    period: "Jul 2026",
    amount: 300,
    status: "open",
    issuedAt: "2026-07-02",
  },
] as const;

describe("Super Admin Billing downloads", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listInvoices).mockResolvedValue(INVOICES as never);
    vi.mocked(api.listTenants).mockResolvedValue([]);
    vi.mocked(exportApi.downloadOperationalExport).mockResolvedValue("invoices.csv");
    vi.mocked(exportApi.downloadInvoicePdf).mockResolvedValue("inv-001.pdf");
  });

  it("exports all filtered invoices as CSV or Excel and downloads real PDFs", async () => {
    const user = userEvent.setup();
    render(<Billing />);
    await screen.findByText("Acme Health");

    await user.type(screen.getByLabelText("Search invoices"), "Acme");
    await user.selectOptions(
      screen.getByLabelText("Filter invoices by status"),
      "paid",
    );
    expect(screen.queryByText("Northwind")).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Export format"), "xlsx");
    await user.click(screen.getByRole("button", { name: "Export" }));
    expect(exportApi.downloadOperationalExport).toHaveBeenCalledWith(
      "invoices",
      "xlsx",
      { search: "Acme", status: "paid" },
    );

    await user.click(screen.getByRole("button", { name: "PDF" }));
    expect(exportApi.downloadInvoicePdf).toHaveBeenCalledWith("inv-001");
  });
});
