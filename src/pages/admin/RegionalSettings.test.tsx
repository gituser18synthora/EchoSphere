/* Regional & Currency Settings — the four geographic/monetary master-data
   sections moved out of Platform Configuration. Coverage: module load, URL
   tabs, the Data Region country catalog, currency catalog rendering,
   exchange-rate CRUD/validation, Order label and status-first ordering.
   The shared MasterPanel pagination behavior is exercised in
   PlatformConfig.test.tsx (single implementation for all tabs). */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RegionalSettings from "@/pages/admin/RegionalSettings";
import { clearProviderCatalogCache } from "@/components/ProviderModelSelect";
import * as api from "@/services/api";

vi.mock("@/services/api", () => ({
  listMaster: vi.fn(),
  createMaster: vi.fn(),
  updateMaster: vi.fn(),
  deleteMaster: vi.fn(),
  duplicatePlan: vi.fn(),
  getMasterAudit: vi.fn(),
  listPlanTenants: vi.fn(),
  setMasterStatus: vi.fn(),
  getProviderCatalog: vi.fn(),
  listProviderModels: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), hasPermission: () => true }),
}));

const listMaster = vi.mocked(api.listMaster);
const createMaster = vi.mocked(api.createMaster);

const paged = (items: Record<string, unknown>[]) => ({
  items,
  meta: { page: 1, pageSize: 25, total: items.length, totalPages: 1 },
});

const COUNTRY_ROWS = [
  { id: 12, iso2: "IN", iso3: "IND", name: "India", region: "Asia", status: "active",
    sortOrder: 0, usageCount: 2, updatedAt: "2026-07-20T10:00:00Z" },
  { id: 28, iso2: "NP", iso3: "NPL", name: "Nepal", region: "Asia", status: "active",
    sortOrder: 1, usageCount: 0, updatedAt: "2026-07-20T10:00:00Z" },
];

const CURRENCY_ROWS = [
  { id: "cur_1", code: "USD", name: "US Dollar", symbol: "$", decimalPlaces: 2,
    isBase: true, status: "active", sortOrder: 0, usageCount: 3 },
  { id: "cur_2", code: "INR", name: "Indian Rupee", symbol: "₹", decimalPlaces: 2,
    isBase: false, status: "active", sortOrder: 1, usageCount: 1 },
  { id: "cur_3", code: "AED", name: "UAE Dirham", symbol: "د.إ", decimalPlaces: 2,
    isBase: false, status: "inactive", sortOrder: 4, usageCount: 0 },
];

const RATE_ROW = {
  id: "fxr_1", name: "USD → INR", baseCode: "USD", targetCode: "INR", rate: "86.50000000",
  effectiveFrom: "2026-07-20T00:00:00Z", source: "manual", status: "active",
  sortOrder: 0, usageCount: 0,
};

const REGION_ROW = {
  id: "dr_1", code: "in-mumbai", name: "Mumbai", country: "India", region: "Asia",
  infrastructureReady: true, status: "active", sortOrder: 7, usageCount: 0,
  updatedAt: "2026-07-20T10:00:00Z",
};

function installMocks() {
  listMaster.mockImplementation((mtype: string) => {
    if (mtype === "countries") return Promise.resolve(paged(COUNTRY_ROWS) as never);
    if (mtype === "currencies") return Promise.resolve(paged(CURRENCY_ROWS) as never);
    if (mtype === "exchange-rates") return Promise.resolve(paged([RATE_ROW]) as never);
    if (mtype === "data-regions") return Promise.resolve(paged([REGION_ROW]) as never);
    return Promise.resolve(paged([]) as never);
  });
  createMaster.mockResolvedValue({ id: "new" } as never);
}

function renderPage(initialPath = "/admin/regional-settings") {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route path="/admin/regional-settings" element={<RegionalSettings />} />
        <Route path="/admin/regional-settings/:tab" element={<RegionalSettings />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  clearProviderCatalogCache();
  installMocks();
});

describe("RegionalSettings — module structure", () => {
  it("loads with the four tabs and Countries as the default", async () => {
    renderPage();
    expect(screen.getByRole("heading", { name: "Regional & Currency Settings" })).toBeInTheDocument();
    expect(screen.getByText(/geographic regions, supported currencies/i)).toBeInTheDocument();
    for (const tab of ["Countries", "Data Regions", "Currencies", "Exchange Rates"]) {
      expect(screen.getByText(tab)).toBeInTheDocument();
    }
    await screen.findByText("India"); // Countries tab content
    expect(listMaster).toHaveBeenCalledWith("countries", expect.anything());
  });

  it("deep links straight to a tab via the URL", async () => {
    renderPage("/admin/regional-settings/currencies");
    await screen.findByText("US Dollar");
    expect(listMaster).toHaveBeenCalledWith("currencies", expect.anything());
  });

  it("falls back to Countries for an unknown tab slug", async () => {
    renderPage("/admin/regional-settings/bogus");
    await screen.findByText("India");
  });
});

describe("RegionalSettings — Countries", () => {
  it("shows the Order column, never Sort order or Updated", async () => {
    renderPage();
    await screen.findByText("India");
    expect(screen.getByRole("columnheader", { name: "Order" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Sort order" })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Updated" })).not.toBeInTheDocument();
  });
});

describe("RegionalSettings — Data Region country catalog", () => {
  async function openAddDataRegion(user: ReturnType<typeof userEvent.setup>) {
    renderPage("/admin/regional-settings/data-regions");
    await screen.findByText("Mumbai");
    await user.click(await screen.findByRole("button", { name: /add data region/i }));
    await screen.findByRole("dialog", { name: "Add data region" });
  }

  it("loads active countries from the country master and locks region to Asia", async () => {
    const user = userEvent.setup();
    await openAddDataRegion(user);
    const country = await screen.findByLabelText("Country");
    await waitFor(() => expect(within(country).getByRole("option", { name: "India (IN / IND)" })).toBeInTheDocument());
    expect(within(country).getByRole("option", { name: "Nepal (NP / NPL)" })).toBeInTheDocument();
    expect(screen.getByLabelText("Region")).toHaveValue("Asia");
    expect(screen.getByLabelText("Region")).toBeDisabled();
    expect(listMaster).toHaveBeenCalledWith("countries", expect.objectContaining({
      includeInactive: false, sortBy: "name",
    }));
  });

  it("submits the selected numeric country ID with the canonical Asia region", async () => {
    const user = userEvent.setup();
    await openAddDataRegion(user);
    await user.type(screen.getByLabelText("Code"), "np-kathmandu");
    await user.type(screen.getByLabelText("Name"), "Nepal – Kathmandu");
    await user.selectOptions(await screen.findByLabelText("Country"), ["28"]);
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(createMaster).toHaveBeenCalledTimes(1));
    expect(createMaster.mock.calls[0][0]).toBe("data-regions");
    expect(createMaster.mock.calls[0][1]).toMatchObject({
      code: "np-kathmandu", name: "Nepal – Kathmandu", countryId: 28, region: "Asia",
    });
  });
});

describe("RegionalSettings — Currencies", () => {
  it("renders the currency catalog with the base tag and symbols", async () => {
    renderPage("/admin/regional-settings/currencies");
    await screen.findByText("US Dollar");
    expect(screen.getByText("Base")).toBeInTheDocument();
    expect(screen.getByText("₹")).toBeInTheDocument();
    // Server order is rendered as-is: the inactive currency stays last.
    const rows = screen.getAllByRole("row").slice(1);
    expect(within(rows[rows.length - 1]).getByText("UAE Dirham")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Order" })).toBeInTheDocument();
    expect(screen.queryByText("Sort order")).not.toBeInTheDocument();
    expect(screen.queryByText("Updated")).not.toBeInTheDocument();
  });

  it("refetches after a status change so a deactivated currency drops to the bottom", async () => {
    const user = userEvent.setup();
    let deactivated = false;
    listMaster.mockImplementation((mtype: string) => {
      if (mtype !== "currencies") return Promise.resolve(paged([]) as never);
      const rows = deactivated
        ? [CURRENCY_ROWS[0], { ...CURRENCY_ROWS[2] }, { ...CURRENCY_ROWS[1], status: "inactive" }]
        : CURRENCY_ROWS;
      return Promise.resolve(paged(rows as never) as never);
    });
    vi.mocked(api.setMasterStatus).mockImplementation(() => {
      deactivated = true;
      return Promise.resolve({} as never);
    });

    renderPage("/admin/regional-settings/currencies");
    await screen.findByText("Indian Rupee");
    const inrRow = screen.getByText("Indian Rupee").closest("tr")!;
    await user.click(within(inrRow).getByRole("button", { name: "Deactivate" }));
    await waitFor(() => expect(api.setMasterStatus).toHaveBeenCalledWith("currencies", "cur_2", "inactive"));
    await waitFor(() => {
      const names = screen.getAllByRole("row").slice(1)
        .map((r) => within(r).queryByText(/US Dollar|Indian Rupee|UAE Dirham/)?.textContent)
        .filter(Boolean);
      expect(names).toEqual(["US Dollar", "UAE Dirham", "Indian Rupee"]);
    });
  });
});

describe("RegionalSettings — Exchange Rates", () => {
  it("adds a USD → INR exchange rate with an explicit rate", async () => {
    const user = userEvent.setup();
    renderPage("/admin/regional-settings/exchange-rates");
    await screen.findByText("86.5"); // the seeded USD → INR row rendered

    await user.click(screen.getByRole("button", { name: /add exchange rate/i }));
    await screen.findByRole("dialog", { name: "Add exchange rate" });

    await user.type(screen.getByLabelText("Exchange rate"), "86.50");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(createMaster).toHaveBeenCalledTimes(1));
    expect(createMaster.mock.calls[0][0]).toBe("exchange-rates");
    expect(createMaster.mock.calls[0][1]).toMatchObject({
      baseCode: "USD", targetCode: "INR", rate: 86.5,
    });
  });

  it("shows field-level validation errors from the API on the rate field", async () => {
    const user = userEvent.setup();
    const apiError = Object.assign(new Error("Validation failed."), {
      fieldErrors: { rate: "Must be greater than zero." },
    });
    createMaster.mockRejectedValueOnce(apiError as never);

    renderPage("/admin/regional-settings/exchange-rates");
    await user.click(await screen.findByRole("button", { name: /add exchange rate/i }));
    await screen.findByRole("dialog", { name: "Add exchange rate" });

    await user.type(screen.getByLabelText("Exchange rate"), "0");
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByText("Must be greater than zero.")).toBeInTheDocument();
  });
});
