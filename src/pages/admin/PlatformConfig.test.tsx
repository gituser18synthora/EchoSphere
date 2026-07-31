import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PlatformConfig from "@/pages/admin/PlatformConfig";
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
const updateMaster = vi.mocked(api.updateMaster);

const paged = (items: Record<string, unknown>[]) => ({
  items,
  meta: { page: 1, pageSize: 25, total: items.length, totalPages: 1 },
});

const PLAN_ROW = {
  id: "pl_1", code: "starter", name: "Starter", status: "active", currency: "USD",
  priceMonthly: 490, priceAnnual: 4900, botLimit: 2, minutesIncluded: 1000,
  seatsIncluded: 5, kbLimit: 5, storageGbIncluded: 5, languagesIncluded: 2,
  concurrentCallLimit: 10, monthlyCallLimit: 0, monthlyTokenLimit: 0,
  monthlyEmbeddingLimit: 0, recordingRetentionDays: 90, transcriptRetentionDays: 90,
  analyticsRetentionDays: 365, isPublic: true, isRecommended: false, sortOrder: 0,
  usageCount: 0, updatedAt: "2026-07-20T10:00:00Z",
};

const VOICE_ROW = {
  id: "vp_1", name: "Shubh", provider: "sarvam", gender: "male", locale: "hi-IN",
  languages: ["hi-IN"], speakingRate: 1, status: "active", sortOrder: 3,
  usageCount: 0, updatedAt: "2026-07-20T10:00:00Z", isDefault: true,
};

const COUNTRY_ROWS = [
  { id: 12, iso2: "IN", iso3: "IND", name: "India", region: "Asia", status: "active", sortOrder: 0 },
  { id: 28, iso2: "NP", iso3: "NPL", name: "Nepal", region: "Asia", status: "active", sortOrder: 1 },
];

function installDefaultMocks() {
  listMaster.mockImplementation((mtype: string) => {
    if (mtype === "plans") return Promise.resolve(paged([PLAN_ROW]) as never);
    if (mtype === "countries") return Promise.resolve(paged(COUNTRY_ROWS) as never);
    if (mtype === "voices") return Promise.resolve(paged([VOICE_ROW]) as never);
    if (mtype === "providers") {
      return Promise.resolve(paged([
        { id: "prov_1", code: "sarvam", name: "Sarvam AI", status: "active", kind: "tts" },
        { id: "prov_2", code: "elevenlabs", name: "ElevenLabs", status: "active", kind: "tts" },
      ]) as never);
    }
    return Promise.resolve(paged([]) as never);
  });
  vi.mocked(api.getProviderCatalog).mockImplementation((capability) =>
    Promise.resolve({ [String(capability)]: [{ code: "openai", name: "OpenAI" }] } as never));
  vi.mocked(api.listProviderModels).mockResolvedValue([] as never);
  createMaster.mockResolvedValue({ id: "pl_new" } as never);
  updateMaster.mockResolvedValue({ id: "pl_1" } as never);
}

async function openAddPlan(user: ReturnType<typeof userEvent.setup>) {
  render(<PlatformConfig />);
  await user.click(screen.getByText("Plans"));
  await user.click(await screen.findByRole("button", { name: /add plan/i }));
  await screen.findByRole("dialog", { name: "Add plan" });
}

describe("PlatformConfig — Add Plan form", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearProviderCatalogCache();
    installDefaultMocks();
  });

  it("renders the currency dropdown with supported currencies", async () => {
    const user = userEvent.setup();
    await openAddPlan(user);
    const currency = screen.getByLabelText("Currency");
    const labels = within(currency).getAllByRole("option").map((o) => o.textContent);
    expect(labels).toEqual([
      "USD · $ US Dollar", "INR · ₹ Indian Rupee", "EUR · € Euro",
      "GBP · £ British Pound", "AED · د.إ UAE Dirham",
    ]);
    expect(currency).toHaveValue("USD"); // default from existing app configuration
  });

  it("submits the selected currency with the plan", async () => {
    const user = userEvent.setup();
    await openAddPlan(user);
    await user.type(screen.getByLabelText("Code"), "new_plan");
    await user.type(screen.getByLabelText("Name"), "New Plan");
    await user.selectOptions(screen.getByLabelText("Currency"), ["INR"]);
    await user.type(screen.getByLabelText("Monthly price"), "999");
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(createMaster).toHaveBeenCalledTimes(1));
    expect(createMaster.mock.calls[0][1]).toMatchObject({
      code: "new_plan", name: "New Plan", currency: "INR", priceMonthly: 999,
    });
  });

  it("numeric fields reject typed negatives and use min=0", async () => {
    const user = userEvent.setup();
    await openAddPlan(user);
    const price = screen.getByLabelText("Monthly price");
    expect(price).toHaveAttribute("min", "0");
    await user.type(price, "-42");
    expect(price).toHaveValue(42);
  });

  it("preserves the draft when the modal closes and reopens", async () => {
    const user = userEvent.setup();
    await openAddPlan(user);
    await user.type(screen.getByLabelText("Name"), "Draft plan");
    await user.type(screen.getByLabelText("Included users"), "7");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /add plan/i }));
    expect(screen.getByLabelText("Name")).toHaveValue("Draft plan");
    expect(screen.getByLabelText("Included users")).toHaveValue(7);
  });

  it("Reset restores the initial default values", async () => {
    const user = userEvent.setup();
    await openAddPlan(user);
    await user.type(screen.getByLabelText("Name"), "Will be reset");
    await user.selectOptions(screen.getByLabelText("Currency"), ["EUR"]);
    await user.click(screen.getByRole("button", { name: "Reset" }));
    expect(screen.getByLabelText("Name")).toHaveValue("");
    expect(screen.getByLabelText("Currency")).toHaveValue("USD");
  });

  it("clears the draft only after a successful submission", async () => {
    const user = userEvent.setup();
    await openAddPlan(user);
    await user.type(screen.getByLabelText("Code"), "ok_plan");
    await user.type(screen.getByLabelText("Name"), "Ok Plan");
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(createMaster).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /add plan/i }));
    expect(screen.getByLabelText("Name")).toHaveValue("");
  });

  it("keeps the form data when submission fails", async () => {
    createMaster.mockRejectedValue(Object.assign(new Error("Validation failed."), {
      fieldErrors: { currency: "Unsupported currency." },
    }));
    const user = userEvent.setup();
    await openAddPlan(user);
    await user.type(screen.getByLabelText("Code"), "fail_plan");
    await user.type(screen.getByLabelText("Name"), "Fail Plan");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText("Validation failed.")).toBeInTheDocument();
    expect(screen.getByText("Unsupported currency.")).toBeInTheDocument();
    // Modal stays open with data intact.
    expect(screen.getByLabelText("Name")).toHaveValue("Fail Plan");
    // Close + reopen: still intact.
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: /add plan/i }));
    expect(screen.getByLabelText("Name")).toHaveValue("Fail Plan");
  });

  it("edit form is populated from the row and never leaks into the add draft", async () => {
    const user = userEvent.setup();
    await openAddPlan(user);
    await user.type(screen.getByLabelText("Name"), "My draft");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await user.click(screen.getByRole("button", { name: "Edit" }));
    await screen.findByRole("dialog", { name: "Edit plan" });
    expect(screen.getByLabelText("Name")).toHaveValue("Starter");
    await user.type(screen.getByLabelText("Name"), " changed");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await user.click(screen.getByRole("button", { name: /add plan/i }));
    expect(screen.getByLabelText("Name")).toHaveValue("My draft");
  });
});

/* Data Region + Countries management moved to Regional & Currency Settings —
   see RegionalSettings.test.tsx for the country-catalog coverage. */

describe("PlatformConfig — regional/monetary sections moved out", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearProviderCatalogCache();
    installDefaultMocks();
  });

  it("no longer offers Countries, Data Regions, Currencies or Exchange Rates tabs", async () => {
    render(<PlatformConfig />);
    await waitFor(() => expect(listMaster).toHaveBeenCalled());
    for (const gone of ["Countries", "Data Regions", "Currencies", "Exchange Rates"]) {
      expect(screen.queryByText(gone)).not.toBeInTheDocument();
    }
    // The product/AI sections all remain.
    for (const kept of ["Industries", "Plans", "AI Profiles", "Providers", "Languages", "Voices", "Provider Pricing"]) {
      expect(screen.getByText(kept)).toBeInTheDocument();
    }
  });
});

describe("PlatformConfig — Voices section", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearProviderCatalogCache();
    installDefaultMocks();
  });

  async function openVoices(user: ReturnType<typeof userEvent.setup>) {
    render(<PlatformConfig />);
    await user.click(screen.getByText("Voices"));
    await screen.findByText("Shubh");
  }

  it("shows Sort order instead of Updated", async () => {
    const user = userEvent.setup();
    await openVoices(user);
    expect(screen.getByText("Sort order")).toBeInTheDocument();
    expect(screen.queryByText("Updated")).not.toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument(); // the row's sortOrder value
  });

  it("provider filter calls the API with the provider and can be cleared", async () => {
    const user = userEvent.setup();
    await openVoices(user);
    await user.selectOptions(await screen.findByLabelText("Filter voices by provider"), ["Sarvam AI"]);
    await waitFor(() => {
      const voiceCalls = listMaster.mock.calls.filter(([mtype]) => mtype === "voices");
      expect(voiceCalls.at(-1)?.[1]).toMatchObject({ provider: "sarvam" });
    });
    expect(screen.getByText("1 filter active")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /clear filters/i }));
    expect(screen.queryByText("1 filter active")).not.toBeInTheDocument();
    await waitFor(() => {
      const voiceCalls = listMaster.mock.calls.filter(([mtype]) => mtype === "voices");
      expect(voiceCalls.at(-1)?.[1]?.provider).toBeUndefined();
    });
  });

  it("filters combine (provider + gender) and show the filtered empty state", async () => {
    const user = userEvent.setup();
    await openVoices(user);
    listMaster.mockImplementation((mtype: string) =>
      Promise.resolve(paged(mtype === "providers" ? [
        { id: "prov_1", code: "sarvam", name: "Sarvam AI", status: "active", kind: "tts" },
      ] : []) as never));
    await user.selectOptions(screen.getByLabelText("Filter voices by provider"), ["Sarvam AI"]);
    await user.selectOptions(screen.getByLabelText("Filter voices by gender"), ["Female"]);
    await waitFor(() => {
      const voiceCalls = listMaster.mock.calls.filter(([mtype]) => mtype === "voices");
      expect(voiceCalls.at(-1)?.[1]).toMatchObject({ provider: "sarvam", gender: "female" });
    });
    expect(await screen.findByText(/no voices match the current filters/i)).toBeInTheDocument();
  });
});

describe("PlatformConfig — Languages section", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearProviderCatalogCache();
    installDefaultMocks();
    listMaster.mockImplementation((mtype: string) =>
      Promise.resolve(paged(mtype === "languages" ? [
        { id: "lang_1", code: "hi-IN", name: "Hindi", nativeName: "हिन्दी", direction: "ltr",
          enabled: true, isDefault: true, sortOrder: 1, usageCount: 0,
          providerSupport: { stt: ["sarvam"], tts: ["sarvam"] },
          updatedAt: "2026-07-20T10:00:00Z" },
      ] : []) as never));
  });

  it("shows Sort order instead of Updated", async () => {
    const user = userEvent.setup();
    render(<PlatformConfig />);
    await user.click(screen.getByText("Languages"));
    await screen.findByText("Hindi");
    expect(screen.getByText("Sort order")).toBeInTheDocument();
    expect(screen.queryByText("Updated")).not.toBeInTheDocument();
  });
});

describe("PlatformConfig — Providers section", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearProviderCatalogCache();
    installDefaultMocks();
  });

  async function openProviders(user: ReturnType<typeof userEvent.setup>) {
    render(<PlatformConfig />);
    await user.click(screen.getByText("Providers"));
  }

  it("shows the Order column (sortOrder) instead of Updated", async () => {
    const user = userEvent.setup();
    listMaster.mockImplementation((mtype: string) =>
      Promise.resolve(paged(mtype === "providers" ? [
        { id: "prov_1", code: "sarvam", name: "Sarvam AI", status: "active", kind: "tts",
          requiresApiKey: true, secretRef: "env:SARVAM_API_KEY", usageCount: 2, sortOrder: 5,
          updatedAt: "2026-07-20T10:00:00Z" },
      ] : []) as never));
    await openProviders(user);
    await screen.findByText("Sarvam AI");
    // Exactly "Order" — never "Sort order" — and no "Updated" column.
    expect(screen.getByRole("columnheader", { name: "Order" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Updated" })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Sort order" })).not.toBeInTheDocument();
    // The real DB-backed sortOrder value is rendered.
    const row = screen.getByText("Sarvam AI").closest("tr")!;
    expect(within(row).getByText("5")).toBeInTheDocument();
  });

  it("moves a deactivated provider to the bottom after the automatic refetch", async () => {
    const user = userEvent.setup();
    const ACTIVE = [
      { id: "prov_a", code: "alpha", name: "Alpha", status: "active", kind: "llm", sortOrder: 0, usageCount: 0 },
      { id: "prov_b", code: "beta", name: "Beta", status: "active", kind: "llm", sortOrder: 1, usageCount: 0 },
    ];
    // Active-first server ordering: once Alpha is inactive it comes back last.
    const REORDERED = [
      { ...ACTIVE[1] },
      { ...ACTIVE[0], status: "inactive" },
    ];
    let deactivated = false;
    listMaster.mockImplementation((mtype: string) => {
      if (mtype !== "providers") return Promise.resolve(paged([]) as never);
      return Promise.resolve(paged(deactivated ? REORDERED : ACTIVE) as never);
    });
    vi.mocked(api.setMasterStatus).mockImplementation(() => {
      deactivated = true;
      return Promise.resolve({} as never);
    });

    await openProviders(user);
    await screen.findByText("Alpha");
    const names = () => screen.getAllByRole("row").slice(1)
      .map((r) => within(r).queryByText(/^(Alpha|Beta)$/)?.textContent)
      .filter(Boolean);
    expect(names()).toEqual(["Alpha", "Beta"]);

    const alphaRow = screen.getByText("Alpha").closest("tr")!;
    await user.click(within(alphaRow).getByRole("button", { name: "Deactivate" }));
    await waitFor(() =>
      expect(api.setMasterStatus).toHaveBeenCalledWith("providers", "prov_a", "inactive"));
    // No manual refresh: the list refetched and Alpha dropped below the active Beta.
    await waitFor(() => expect(names()).toEqual(["Beta", "Alpha"]));
  });

  it("submits an edited Order value and refetches the list", async () => {
    const user = userEvent.setup();
    listMaster.mockImplementation((mtype: string) =>
      Promise.resolve(paged(mtype === "providers" ? [
        { id: "prov_1", code: "sarvam", name: "Sarvam AI", status: "active", kind: "tts",
          requiresApiKey: true, secretRef: "env:SARVAM_API_KEY", usageCount: 0, sortOrder: 5 },
      ] : []) as never));
    updateMaster.mockResolvedValue({ id: "prov_1" } as never);
    await openProviders(user);
    await screen.findByText("Sarvam AI");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await screen.findByRole("dialog", { name: "Edit provider" });
    const order = screen.getByLabelText("Sort order");
    await user.clear(order);
    await user.type(order, "2");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(updateMaster).toHaveBeenCalledTimes(1));
    expect(updateMaster.mock.calls[0][0]).toBe("providers");
    expect(updateMaster.mock.calls[0][2]).toMatchObject({ sortOrder: 2 });
  });
});

describe("PlatformConfig — status-first ordering applies module-wide", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearProviderCatalogCache();
    installDefaultMocks();
  });

  // The shared MasterPanel refetches after every status change, so the
  // backend's status-first order is reflected on ALL tabs — not just providers.
  it("moves a deactivated industry to the bottom after refetch (non-provider tab)", async () => {
    const user = userEvent.setup();
    const ACTIVE = [
      { id: "ind_a", code: "alpha", name: "Alpha", status: "active", sortOrder: 0, usageCount: 0, description: "" },
      { id: "ind_b", code: "beta", name: "Beta", status: "active", sortOrder: 1, usageCount: 0, description: "" },
    ];
    const REORDERED = [{ ...ACTIVE[1] }, { ...ACTIVE[0], status: "inactive" }];
    let deactivated = false;
    listMaster.mockImplementation((mtype: string) => {
      if (mtype !== "industries") return Promise.resolve(paged([]) as never);
      return Promise.resolve(paged(deactivated ? REORDERED : ACTIVE) as never);
    });
    vi.mocked(api.setMasterStatus).mockImplementation(() => {
      deactivated = true;
      return Promise.resolve({} as never);
    });

    render(<PlatformConfig />); // Industries is the default tab
    await screen.findByText("Alpha");
    const names = () => screen.getAllByRole("row").slice(1)
      .map((r) => within(r).queryByText(/^(Alpha|Beta)$/)?.textContent)
      .filter(Boolean);
    expect(names()).toEqual(["Alpha", "Beta"]);

    const alphaRow = screen.getByText("Alpha").closest("tr")!;
    await user.click(within(alphaRow).getByRole("button", { name: "Deactivate" }));
    await waitFor(() =>
      expect(api.setMasterStatus).toHaveBeenCalledWith("industries", "ind_a", "inactive"));
    await waitFor(() => expect(names()).toEqual(["Beta", "Alpha"]));
  });
});

describe("PlatformConfig — Order column replaces Updated (Industries/Plans/AI Profiles)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearProviderCatalogCache();
    installDefaultMocks();
  });

  // One row per section, each carrying a real DB-backed sortOrder (7) AND an
  // updatedAt — proving the table shows Order (not the timestamp) and never a
  // "Updated" or "Sort order" header. (Data Regions moved to Regional &
  // Currency Settings — covered in RegionalSettings.test.tsx.)
  const SECTIONS = [
    {
      tab: "Industries", mtype: "industries",
      row: { id: "ind_1", code: "banking", name: "Banking", description: "Financial services",
        status: "active", sortOrder: 7, usageCount: 0, updatedAt: "2026-07-20T10:00:00Z" },
    },
    {
      tab: "Plans", mtype: "plans",
      row: { ...PLAN_ROW, id: "pl_ord", name: "Growth", code: "growth", sortOrder: 7 },
    },
    {
      tab: "AI Profiles", mtype: "ai-profiles",
      row: { id: "aip_1", code: "balanced", name: "Balanced", costCategory: "medium",
        llmProvider: "openai", llmModel: "gpt-4o-mini", status: "active", sortOrder: 7,
        usageCount: 0, updatedAt: "2026-07-20T10:00:00Z" },
    },
  ] as const;

  it.each(SECTIONS)("$tab shows the Order column (sortOrder), never Updated or Sort order",
    async ({ tab, mtype, row }) => {
      const user = userEvent.setup();
      listMaster.mockImplementation((mt: string) =>
        Promise.resolve(paged(mt === mtype ? [row as Record<string, unknown>] : []) as never));
      render(<PlatformConfig />);
      if (tab !== "Industries") await user.click(screen.getByText(tab));
      await screen.findByText(row.name);

      expect(screen.getByRole("columnheader", { name: "Order" })).toBeInTheDocument();
      expect(screen.queryByRole("columnheader", { name: "Updated" })).not.toBeInTheDocument();
      expect(screen.queryByRole("columnheader", { name: "Updated At" })).not.toBeInTheDocument();
      expect(screen.queryByRole("columnheader", { name: "Sort order" })).not.toBeInTheDocument();
      // The real DB-backed sortOrder value is rendered in the row (not the date).
      const tr = screen.getByText(row.name).closest("tr")!;
      expect(within(tr).getByText("7")).toBeInTheDocument();
      expect(within(tr).queryByText(/2026/)).not.toBeInTheDocument();
    });

  it.each(SECTIONS)("$tab add form labels the order field 'Order', not 'Sort order'",
    async ({ tab, mtype, row }) => {
      const user = userEvent.setup();
      listMaster.mockImplementation((mt: string) =>
        Promise.resolve(paged(mt === mtype ? [row as Record<string, unknown>] : []) as never));
      render(<PlatformConfig />);
      if (tab !== "Industries") await user.click(screen.getByText(tab));
      await screen.findByText(row.name);
      await user.click(await screen.findByRole("button", { name: new RegExp(`add`, "i") }));
      await screen.findByRole("dialog");
      expect(screen.getByLabelText("Order")).toBeInTheDocument();
      expect(screen.queryByLabelText("Sort order")).not.toBeInTheDocument();
    });
});

describe("PlatformConfig — Plans table Limits/Flags layout", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearProviderCatalogCache();
    installDefaultMocks();
  });

  async function goPlans(user: ReturnType<typeof userEvent.setup>, rows: Record<string, unknown>[]) {
    listMaster.mockImplementation((mt: string) =>
      Promise.resolve(paged(mt === "plans" ? rows : []) as never));
    render(<PlatformConfig />);
    await user.click(screen.getByText("Plans"));
  }

  it("renders limits as readable key-value metrics (large values formatted, never clipped)", async () => {
    const user = userEvent.setup();
    const row = { ...PLAN_ROW, id: "pl_big", name: "Scale", code: "scale",
      botLimit: 25, minutesIncluded: 500000, seatsIncluded: 100 };
    await goPlans(user, [row]);
    const tr = (await screen.findByText("Scale")).closest("tr")!;
    const limits = tr.querySelector(".plan-limits")!;
    expect(limits).toBeTruthy();                         // structured, not one raw string
    expect(limits.querySelectorAll(".limit")).toHaveLength(3);
    expect(limits.textContent).toContain("25 bots");
    expect(limits.textContent).toContain("500,000 min"); // locale-formatted, readable
    expect(limits.textContent).toContain("100 seats");
  });

  it("gives Limits the widest column and Flags a minimal one", async () => {
    const user = userEvent.setup();
    await goPlans(user, [PLAN_ROW]);
    await screen.findByText("Starter");
    expect(screen.getByRole("columnheader", { name: "Limits" }).style.width).toBe("280px");
    expect(screen.getByRole("columnheader", { name: "Flags" }).style.width).toBe("1px");
  });

  it("renders multiple flags as compact pills that can wrap", async () => {
    const user = userEvent.setup();
    const row = { ...PLAN_ROW, id: "pl_flags", name: "Featured", code: "featured",
      isRecommended: true, isPublic: false };
    await goPlans(user, [row]);
    const tr = (await screen.findByText("Featured")).closest("tr")!;
    const flags = tr.querySelector(".plan-flags")!;
    expect(flags).toBeTruthy();
    expect(flags.querySelectorAll(".tag")).toHaveLength(2);
    expect(flags.textContent).toContain("Recommended");
    expect(flags.textContent).toContain("Hidden");
  });

  it("shows the em-dash empty state when a plan has no flags", async () => {
    const user = userEvent.setup();
    const row = { ...PLAN_ROW, id: "pl_plain", name: "Basic", code: "basic",
      isRecommended: false, isPublic: true };
    await goPlans(user, [row]);
    const tr = (await screen.findByText("Basic")).closest("tr")!;
    expect(tr.querySelector(".plan-flags")).toBeFalsy();
    expect(within(tr).getByText("—")).toBeInTheDocument(); // flags cell empty state
  });

  it("shows em-dash empty states when a plan has no limit values or flags", async () => {
    const user = userEvent.setup();
    const row = { ...PLAN_ROW, id: "pl_blank", name: "Blank", code: "blank",
      botLimit: null, minutesIncluded: null, seatsIncluded: null,
      isRecommended: false, isPublic: true };
    await goPlans(user, [row]);
    const tr = (await screen.findByText("Blank")).closest("tr")!;
    expect(tr.querySelector(".plan-limits")).toBeFalsy();
    expect(within(tr).getAllByText("—")).toHaveLength(2); // limits + flags empty states
  });

  it("preserves the row order the backend returns (status-first ordering unchanged)", async () => {
    const user = userEvent.setup();
    const rows = [
      { ...PLAN_ROW, id: "pl_act", name: "ActivePlan", code: "act", status: "active", sortOrder: 1 },
      { ...PLAN_ROW, id: "pl_ina", name: "InactivePlan", code: "ina", status: "inactive", sortOrder: 0 },
    ];
    await goPlans(user, [rows[0], rows[1]]);
    await screen.findByText("ActivePlan");
    const names = screen.getAllByRole("row").slice(1)
      .map((r) => within(r).queryByText(/^(ActivePlan|InactivePlan)$/)?.textContent)
      .filter(Boolean);
    expect(names).toEqual(["ActivePlan", "InactivePlan"]); // frontend renders backend order as-is
  });
});

/* ---------- Pagination keeps the current page across mutations ----------
   All Platform Configuration tabs share MasterPanel, so the industries and
   providers coverage below exercises the single implementation used by
   Industries, Countries, Data Regions, Plans, AI Profiles, Providers,
   Languages and Voices alike. */

describe("PlatformConfig — pagination keeps the current page across mutations", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearProviderCatalogCache();
    installDefaultMocks();
  });

  const industry = (i: number, status = "active") => ({
    id: `ind_${i}`, code: `code_${i}`, name: `Industry ${String(i).padStart(2, "0")}`,
    status, sortOrder: i, usageCount: 0, description: "",
  });

  /** Server-faithful industries mock: status-first + sortOrder global ordering
      applied BEFORE slicing (the backend contract), search filter, real meta. */
  function installPagedIndustries(count: number, initialStatus: Record<number, string> = {}) {
    let dataset = Array.from({ length: count }, (_, i) => industry(i + 1, initialStatus[i + 1] ?? "active"));
    listMaster.mockImplementation(((mtype: string, opts?: { page?: number; pageSize?: number; search?: string }) => {
      if (mtype !== "industries") return Promise.resolve(paged([]) as never);
      const page = opts?.page ?? 1;
      const pageSize = opts?.pageSize ?? 25;
      const filtered = opts?.search
        ? dataset.filter((r) => r.name.toLowerCase().includes(opts.search!.toLowerCase()))
        : dataset;
      const sorted = [...filtered].sort((a, b) =>
        (a.status === "active" ? 0 : 1) - (b.status === "active" ? 0 : 1) || a.sortOrder - b.sortOrder);
      return Promise.resolve({
        items: sorted.slice((page - 1) * pageSize, page * pageSize),
        meta: { page, pageSize, total: filtered.length, totalPages: Math.max(1, Math.ceil(filtered.length / pageSize)) },
      } as never);
    }) as never);
    return {
      setStatus: (id: string, status: string) => { dataset = dataset.map((r) => (r.id === id ? { ...r, status } : r)); },
      remove: (id: string) => { dataset = dataset.filter((r) => r.id !== id); },
      rename: (id: string, name: string) => { dataset = dataset.map((r) => (r.id === id ? { ...r, name } : r)); },
    };
  }

  const rowNames = () => screen.getAllByRole("row").slice(1)
    .map((r) => within(r).queryByText(/^Industry \d+( renamed)?$/)?.textContent)
    .filter(Boolean);

  async function goToPage2(user: ReturnType<typeof userEvent.setup>, firstPage2Row: string) {
    render(<PlatformConfig />); // Industries is the default tab
    await screen.findByText("Industry 01");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText(firstPage2Row);
  }

  it("deactivating a record on page 2 keeps page 2 selected and refetches its rows", async () => {
    const user = userEvent.setup();
    const ds = installPagedIndustries(30);
    vi.mocked(api.setMasterStatus).mockImplementation(((_m: string, id: string, status: string) => {
      ds.setStatus(String(id), status);
      return Promise.resolve({} as never);
    }) as never);

    await goToPage2(user, "Industry 26");
    expect(screen.getByText(/page 2 of 2/)).toBeInTheDocument();
    expect(rowNames()).toEqual(["Industry 26", "Industry 27", "Industry 28", "Industry 29", "Industry 30"]);

    const row = screen.getByText("Industry 26").closest("tr")!;
    await user.click(within(row).getByRole("button", { name: "Deactivate" }));
    await waitFor(() => expect(api.setMasterStatus).toHaveBeenCalledWith("industries", "ind_26", "inactive"));

    // The refetch targets the page the user is on — never page 1 — and the
    // status-first reorder drops the deactivated row to the page tail.
    await waitFor(() =>
      expect(rowNames()).toEqual(["Industry 27", "Industry 28", "Industry 29", "Industry 30", "Industry 26"]));
    expect(listMaster).toHaveBeenLastCalledWith("industries", expect.objectContaining({ page: 2 }));
    expect(screen.getByText(/page 2 of 2/)).toBeInTheDocument();
    expect(screen.queryByText("Industry 01")).not.toBeInTheDocument(); // page-1 rows never bleed in
  });

  it("activating a record on page 2 keeps page 2 selected and shows the reordered rows", async () => {
    const user = userEvent.setup();
    const ds = installPagedIndustries(30, { 26: "inactive" });
    vi.mocked(api.setMasterStatus).mockImplementation(((_m: string, id: string, status: string) => {
      ds.setStatus(String(id), status);
      return Promise.resolve({} as never);
    }) as never);

    await goToPage2(user, "Industry 27"); // actives first: 27–30, then inactive 26
    expect(rowNames()).toEqual(["Industry 27", "Industry 28", "Industry 29", "Industry 30", "Industry 26"]);

    const row = screen.getByText("Industry 26").closest("tr")!;
    await user.click(within(row).getByRole("button", { name: "Activate" }));
    await waitFor(() => expect(api.setMasterStatus).toHaveBeenCalledWith("industries", "ind_26", "active"));

    await waitFor(() =>
      expect(rowNames()).toEqual(["Industry 26", "Industry 27", "Industry 28", "Industry 29", "Industry 30"]));
    expect(listMaster).toHaveBeenLastCalledWith("industries", expect.objectContaining({ page: 2 }));
    expect(screen.getByText(/page 2 of 2/)).toBeInTheDocument();
  });

  it("editing a record on page 2 keeps page 2 selected and shows the refetched row", async () => {
    const user = userEvent.setup();
    const ds = installPagedIndustries(30);
    updateMaster.mockImplementation(((_m: string, id: string, payload: Record<string, unknown>) => {
      ds.rename(String(id), String(payload.name));
      return Promise.resolve({} as never);
    }) as never);

    await goToPage2(user, "Industry 26");
    const row = screen.getByText("Industry 26").closest("tr")!;
    await user.click(within(row).getByRole("button", { name: "Edit" }));
    await screen.findByRole("dialog", { name: "Edit industry" });
    const name = screen.getByLabelText("Name");
    await user.clear(name);
    await user.type(name, "Industry 26 renamed");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(updateMaster).toHaveBeenCalledTimes(1));

    await screen.findByText("Industry 26 renamed");
    expect(listMaster).toHaveBeenLastCalledWith("industries", expect.objectContaining({ page: 2 }));
    expect(screen.getByText(/page 2 of 2/)).toBeInTheDocument();
    expect(screen.queryByText("Industry 01")).not.toBeInTheDocument();
  });

  it("moves to the nearest valid page when the current page empties", async () => {
    const user = userEvent.setup();
    const ds = installPagedIndustries(26); // page 2 holds exactly one row
    vi.mocked(api.deleteMaster).mockImplementation(((_m: string, id: string) => {
      ds.remove(String(id));
      return Promise.resolve({} as never);
    }) as never);

    await goToPage2(user, "Industry 26");
    expect(screen.getByText(/page 2 of 2/)).toBeInTheDocument();

    const row = screen.getByText("Industry 26").closest("tr")!;
    await user.click(within(row).getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete industry?" });
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.deleteMaster).toHaveBeenCalledWith("industries", "ind_26"));

    // Page 2 no longer exists → the panel lands on the highest remaining page.
    await waitFor(() =>
      expect(listMaster).toHaveBeenLastCalledWith("industries", expect.objectContaining({ page: 1 })));
    await screen.findByText("Industry 01");
    expect(screen.queryByText(/page 2 of/)).not.toBeInTheDocument();
    expect(screen.queryByText("Industry 26")).not.toBeInTheDocument(); // no stale rows
  });

  it("search changes reset to page 1", async () => {
    const user = userEvent.setup();
    installPagedIndustries(30);
    await goToPage2(user, "Industry 26");

    await user.type(screen.getByLabelText("Search industries"), "Industry 03");
    await waitFor(() =>
      expect(listMaster).toHaveBeenLastCalledWith("industries",
        expect.objectContaining({ page: 1, search: "Industry 03" })), { timeout: 3000 });
    await screen.findByText("Industry 03");
    expect(screen.queryByText(/page 2 of/)).not.toBeInTheDocument();
  });

  it("filter changes reset to page 1 (providers kind filter)", async () => {
    const user = userEvent.setup();
    const providers = Array.from({ length: 30 }, (_, i) => ({
      id: `prov_${i + 1}`, code: `p${i + 1}`, name: `Provider ${String(i + 1).padStart(2, "0")}`,
      status: "active", kind: "tts", sortOrder: i + 1, usageCount: 0,
    }));
    listMaster.mockImplementation(((mtype: string, opts?: { page?: number; pageSize?: number; kind?: string }) => {
      if (mtype !== "providers") return Promise.resolve(paged([]) as never);
      const page = opts?.page ?? 1;
      const pageSize = opts?.pageSize ?? 25;
      const filtered = opts?.kind ? providers.filter((p) => p.kind === opts.kind) : providers;
      return Promise.resolve({
        items: filtered.slice((page - 1) * pageSize, page * pageSize),
        meta: { page, pageSize, total: filtered.length, totalPages: Math.max(1, Math.ceil(filtered.length / pageSize)) },
      } as never);
    }) as never);

    render(<PlatformConfig />);
    await user.click(screen.getByText("Providers"));
    await screen.findByText("Provider 01");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("Provider 26");

    await user.selectOptions(screen.getByLabelText("Filter by provider kind"), ["tts"]);
    await waitFor(() =>
      expect(listMaster).toHaveBeenLastCalledWith("providers",
        expect.objectContaining({ page: 1, kind: "tts" })));
    await screen.findByText("Provider 01");
  });

  it("ignores a second click while a status mutation is still in flight", async () => {
    const user = userEvent.setup();
    installPagedIndustries(3);
    let resolveMutation: (() => void) | undefined;
    vi.mocked(api.setMasterStatus).mockImplementation((() =>
      new Promise((resolve) => { resolveMutation = () => resolve({} as never); })) as never);

    render(<PlatformConfig />);
    const row = (await screen.findByText("Industry 01")).closest("tr")!;
    const button = within(row).getByRole("button", { name: "Deactivate" });
    await user.click(button);
    await user.click(button);
    expect(api.setMasterStatus).toHaveBeenCalledTimes(1);
    resolveMutation?.();
    // Let the pending refetch settle inside the test lifecycle.
    await waitFor(() => expect(listMaster).toHaveBeenLastCalledWith(
      "industries", expect.objectContaining({ page: 1 })));
  });
});

/* ---------- Provider Pricing (Currencies / Exchange Rates moved to
   Regional & Currency Settings — see RegionalSettings.test.tsx) ---------- */

const PRICING_ROW = {
  id: "ppr_1", providerCode: "openai", capability: "llm", modelCode: "gpt-4o-mini",
  component: "tokens", unit: "per_1k_tokens", unitPrice: "0.0006000000",
  sellingPrice: null, currencyCode: "USD", effectiveFrom: "2026-07-20T00:00:00Z",
  status: "active", sortOrder: 0, usageCount: 0, name: "openai/gpt-4o-mini · tokens",
};

const PRICING_ROW_INR = {
  id: "ppr_2", providerCode: "sarvam", capability: "stt", modelCode: "saarika:v2.5",
  component: "audio_seconds", unit: "per_hour", unitPrice: "30.0000000000",
  sellingPrice: "45.0000000000", currencyCode: "INR",
  effectiveFrom: "2026-07-20T00:00:00Z", status: "active",
  sortOrder: 0, usageCount: 0, name: "sarvam/saarika:v2.5 · audio_seconds",
};

/* OpenAI quotes text models per 1M tokens split by component — the shape the
   seeded LLM prices actually use. */
const PRICING_ROW_PER_1M = {
  id: "ppr_3", providerCode: "openai", capability: "llm", modelCode: "gpt-4.1-mini",
  component: "output_tokens", unit: "per_1m_tokens", unitPrice: "1.6000000000",
  sellingPrice: null, currencyCode: "USD", effectiveFrom: "2026-07-31T00:00:00Z",
  status: "active", sortOrder: 0, usageCount: 0,
  name: "openai/gpt-4.1-mini · output_tokens",
};

const PRICING_PROVIDERS = [
  { id: "prov_1", code: "sarvam", name: "Sarvam AI", status: "active", kind: "tts" },
  { id: "prov_2", code: "elevenlabs", name: "ElevenLabs", status: "active", kind: "tts" },
  { id: "prov_3", code: "deepgram", name: "Deepgram", status: "inactive", kind: "stt" },
];

describe("Provider pricing configuration", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearProviderCatalogCache();
    installDefaultMocks();
    listMaster.mockImplementation((mtype: string) => {
      if (mtype === "provider-pricing") {
        return Promise.resolve(
          paged([PRICING_ROW, PRICING_ROW_INR, PRICING_ROW_PER_1M]) as never,
        );
      }
      if (mtype === "providers") return Promise.resolve(paged(PRICING_PROVIDERS) as never);
      if (mtype === "provider-models") {
        return Promise.resolve(paged([
          { id: "pm_1", code: "bulbul:v3", name: "Bulbul v3 (streaming)", status: "active" },
          { id: "pm_2", code: "bulbul:v2", name: "Bulbul v2 (legacy)", status: "inactive" },
        ]) as never);
      }
      return Promise.resolve(paged([]) as never);
    });
    createMaster.mockResolvedValue({ id: "new" } as never);
  });

  async function openPricingTab(user: ReturnType<typeof userEvent.setup>) {
    render(<PlatformConfig />);
    await user.click(screen.getByText("Provider Pricing"));
    await screen.findAllByText("openai");  // one row per priced component
  }

  it("renders human-readable units and the tenant price column", async () => {
    const user = userEvent.setup();
    await openPricingTab(user);
    expect(screen.getByRole("columnheader", { name: /provider cost/i })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /tenant price/i })).toBeInTheDocument();
    // $0.0006 per 1K tokens — the unit is spelled out, never a raw code.
    expect(screen.getByText("per 1K tokens")).toBeInTheDocument();
    expect(screen.queryByText("per_1k_tokens")).not.toBeInTheDocument();
    // INR per-hour row shows both the provider cost and the selling price.
    expect(screen.getAllByText("per hour")).toHaveLength(2);
    // Split per-1M-token prices (the OpenAI shape) get their own label.
    expect(screen.getByText("per 1M tokens")).toBeInTheDocument();
    expect(screen.queryByText("per_1m_tokens")).not.toBeInTheDocument();
  });

  it("formats prices as money without dropping sub-cent precision", async () => {
    const user = userEvent.setup();
    await openPricingTab(user);
    // 1.6 is a price, not a number: two decimals minimum.
    expect(screen.getByText(/^\$1\.60$/)).toBeInTheDocument();
    expect(screen.getByText(/^₹30\.00$/)).toBeInTheDocument();
    // …but a rate finer than a cent is never rounded away.
    expect(screen.getByText(/^\$0\.0006$/)).toBeInTheDocument();
  });

  it("adds a price through capability-driven provider/model selects", async () => {
    const user = userEvent.setup();
    await openPricingTab(user);
    await user.click(screen.getByRole("button", { name: /add provider price/i }));
    const dialog = await screen.findByRole("dialog", { name: "Add provider price" });
    const form = within(dialog);

    await user.selectOptions(form.getByLabelText("Capability"), "tts");
    const providerSelect = form.getByLabelText("Provider");
    await waitFor(() => expect(providerSelect).toBeEnabled());
    await user.selectOptions(providerSelect, "sarvam");
    const modelSelect = form.getByLabelText("Model");
    await waitFor(() => expect(modelSelect).toBeEnabled());
    // Governance-inactive catalog models stay priceable, labelled as inactive.
    expect(within(modelSelect).getByRole("option", { name: /Bulbul v2 \(legacy\) \(inactive\)/ })).toBeInTheDocument();
    await user.selectOptions(modelSelect, "bulbul:v3");

    await user.selectOptions(form.getByLabelText("Component"), "characters");
    await user.selectOptions(form.getByLabelText("Pricing unit"), "per_1k_characters");
    await user.type(form.getByLabelText("Provider cost"), "3");
    await user.type(form.getByLabelText("Tenant price"), "4.5");
    await user.selectOptions(form.getByLabelText("Price currency"), "INR");
    await user.click(form.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(createMaster).toHaveBeenCalledTimes(1));
    expect(createMaster.mock.calls[0][0]).toBe("provider-pricing");
    expect(createMaster.mock.calls[0][1]).toMatchObject({
      capability: "tts", providerCode: "sarvam", modelCode: "bulbul:v3",
      component: "characters", unit: "per_1k_characters",
      unitPrice: 3, sellingPrice: 4.5, currencyCode: "INR",
    });
  });

  it("offers the per-1M-characters and per-hour units", async () => {
    const user = userEvent.setup();
    await openPricingTab(user);
    await user.click(screen.getByRole("button", { name: /add provider price/i }));
    const dialog = await screen.findByRole("dialog", { name: "Add provider price" });
    const unit = within(dialog).getByLabelText("Pricing unit");
    const labels = within(unit).getAllByRole("option").map((o) => o.textContent);
    expect(labels).toContain("per 1M characters");
    expect(labels).toContain("per hour");
    expect(labels).toContain("per minute");
  });

  it("a capability change clears the dependent provider/model selections", async () => {
    const user = userEvent.setup();
    await openPricingTab(user);
    await user.click(screen.getByRole("button", { name: /add provider price/i }));
    const dialog = await screen.findByRole("dialog", { name: "Add provider price" });
    const form = within(dialog);

    await user.selectOptions(form.getByLabelText("Capability"), "tts");
    const providerSelect = form.getByLabelText("Provider");
    await waitFor(() => expect(providerSelect).toBeEnabled());
    await user.selectOptions(providerSelect, "sarvam");
    expect(providerSelect).toHaveValue("sarvam");

    await user.selectOptions(form.getByLabelText("Capability"), "stt");
    await waitFor(() => expect(form.getByLabelText("Provider")).not.toHaveValue("sarvam"));
  });

  it("telephony providers stay free-form codes", async () => {
    const user = userEvent.setup();
    await openPricingTab(user);
    await user.click(screen.getByRole("button", { name: /add provider price/i }));
    const dialog = await screen.findByRole("dialog", { name: "Add provider price" });
    const form = within(dialog);
    await user.selectOptions(form.getByLabelText("Capability"), "telephony");
    const provider = form.getByLabelText("Provider");
    expect(provider.tagName).toBe("INPUT");
    await user.type(provider, "twilio");
    expect(provider).toHaveValue("twilio");
  });

  it("filters the list by capability and provider server-side", async () => {
    const user = userEvent.setup();
    await openPricingTab(user);

    await user.selectOptions(screen.getByLabelText("Filter prices by capability"), "stt");
    await waitFor(() => {
      const call = listMaster.mock.calls.filter(([m]) => m === "provider-pricing").at(-1);
      expect(call?.[1]).toMatchObject({ capability: "stt" });
    });

    // With STT selected only STT-capable providers are offered.
    const providerFilter = screen.getByLabelText("Filter prices by provider");
    const names = within(providerFilter).getAllByRole("option").map((o) => o.textContent);
    expect(names).toContain("Deepgram");
    expect(names).not.toContain("ElevenLabs");

    await user.selectOptions(providerFilter, "deepgram");
    await waitFor(() => {
      const call = listMaster.mock.calls.filter(([m]) => m === "provider-pricing").at(-1);
      expect(call?.[1]).toMatchObject({ capability: "stt", provider: "deepgram" });
    });
    expect(screen.getByText("2 filters active")).toBeInTheDocument();
  });
});
