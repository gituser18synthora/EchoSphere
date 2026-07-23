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

describe("PlatformConfig — Data Region country catalog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearProviderCatalogCache();
    installDefaultMocks();
  });

  async function openAddDataRegion(user: ReturnType<typeof userEvent.setup>) {
    render(<PlatformConfig />);
    await user.click(screen.getByText("Data Regions"));
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
