/* Tenant Copy/Paste deployment UI:
   - TenantDetail → "Export Tenant JSON" downloads tenant_<id>.json
   - Organizations → "Import Tenant JSON" uploads that file, previews the
     package, imports it PRESERVING ids, then refreshes the tenant list
   - parse/backend errors surface their exact messages, never a generic one */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TenantDetail from "@/pages/admin/TenantDetail";
import Organizations from "@/pages/admin/Organizations";
import * as api from "@/services/api";

const { toastSpy } = vi.hoisted(() => ({ toastSpy: vi.fn() }));

vi.mock("@/services/api", () => ({
  getTenant: vi.fn(),
  getOnboardingOptions: vi.fn(),
  updateTenant: vi.fn(),
  getTenantAnalytics: vi.fn(),
  listAudit: vi.fn(),
  listBots: vi.fn(),
  listKnowledge: vi.fn(),
  listReleases: vi.fn(),
  listSubscriptions: vi.fn(),
  listTeam: vi.fn(),
  resetUserPassword: vi.fn(),
  listTenants: vi.fn(),
  simulateAction: vi.fn(),
  exportTenantPackage: vi.fn(),
  importTenantPackage: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: toastSpy, hasPermission: () => true }),
}));

const TENANT = {
  id: "tn_620d5400d462", name: "Honasa Care", code: "honasa", domain: "honasa.example",
  industry: "retail", region: "in-mumbai", aiProfileCode: "balanced",
  defaultLanguages: ["en-IN"], plan: "growth", status: "active",
  createdAt: "2026-01-01T00:00:00Z", users: 1, bots: 1, callsMonth: 0,
  minutesMonth: 0, mrr: 0, aiCostMonth: 0, health: "good",
  adminEmail: "admin@honasa.example",
};

const PACKAGE = {
  kind: "echosphere.tenant.export",
  schema_version: 1,
  resources: {
    tenant: { id: "tn_620d5400d462", name: "Honasa Care" },
    bots: [{ id: "bot_71194477c0eb", name: "Customer Care Bot" }],
    workflows: [{ id: "wf_1" }],
    prompts: [{ id: "pr_1" }],
    intents: [{ id: "in_1" }, { id: "in_2" }],
    api_connections: [{ id: "api_1" }],
    channel_configs: [{ id: "ch_1" }],
    knowledge_sources: [{ id: "ks_1" }],
  },
  knowledge_plane: { documents: [{ id: "kdoc_1" }] },
};

const REPORT = {
  tenantId: "tn_620d5400d462",
  created: { tenant: 1, bot: 3, workflow: 1 },
  updated: {},
  reused: { guardrail_profile: 1 },
  remappedIds: {},
  warnings: [],
  knowledgeDocuments: 1,
};

function packageFile(content: string, name = "tenant_tn_620d5400d462.json"): File {
  return new File([content], name, { type: "application/json" });
}

beforeEach(() => {
  vi.clearAllMocks();
  URL.createObjectURL = vi.fn(() => "blob:tenant-package");
  URL.revokeObjectURL = vi.fn();
});

describe("TenantDetail — Export Tenant JSON", () => {
  beforeEach(() => {
    vi.mocked(api.getTenant).mockResolvedValue(TENANT as never);
    vi.mocked(api.getTenantAnalytics).mockResolvedValue({ callsSeries: [] } as never);
    vi.mocked(api.exportTenantPackage).mockResolvedValue(PACKAGE as never);
  });

  it("downloads the complete package as tenant_<id>.json", async () => {
    const downloads: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
      function (this: HTMLAnchorElement) { downloads.push(this.download); },
    );
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/admin/tenants/tn_620d5400d462"]}>
        <Routes><Route path="/admin/tenants/:tenantId" element={<TenantDetail />} /></Routes>
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole("button", { name: "Export Tenant JSON" }));

    await waitFor(() => {
      expect(api.exportTenantPackage).toHaveBeenCalledWith("tn_620d5400d462");
      expect(downloads).toEqual(["tenant_tn_620d5400d462.json"]);
    });
    expect(toastSpy).toHaveBeenCalledWith(
      expect.stringContaining("tenant_tn_620d5400d462.json downloaded"),
    );
  });

  it("surfaces the backend error when the export fails", async () => {
    vi.mocked(api.exportTenantPackage).mockRejectedValue(new Error("Tenant not found"));
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/admin/tenants/tn_620d5400d462"]}>
        <Routes><Route path="/admin/tenants/:tenantId" element={<TenantDetail />} /></Routes>
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole("button", { name: "Export Tenant JSON" }));
    await waitFor(() => expect(toastSpy).toHaveBeenCalledWith("Tenant not found", "error"));
  });
});

describe("Organizations — Import Tenant JSON", () => {
  beforeEach(() => {
    vi.mocked(api.listTenants).mockResolvedValue([] as never);
  });

  const openImportDialog = async (user: ReturnType<typeof userEvent.setup>) => {
    render(<MemoryRouter><Organizations /></MemoryRouter>);
    await user.click(await screen.findByRole("button", { name: "Import Tenant JSON" }));
    return screen.findByRole("dialog", { name: "Import Tenant JSON" });
  };

  it("previews the selected file, imports it verbatim, and refreshes the list", async () => {
    vi.mocked(api.importTenantPackage).mockResolvedValue(REPORT as never);
    const user = userEvent.setup();
    const dialog = await openImportDialog(user);

    await user.upload(
      within(dialog).getByLabelText("Choose tenant JSON file"),
      packageFile(JSON.stringify(PACKAGE)),
    );

    // Package preview: tenant identity and what the file contains.
    await within(dialog).findByText("Honasa Care — ready to import");
    expect(within(dialog).getByText("tn_620d5400d462")).toBeInTheDocument();
    expect(within(dialog).getByText(/Customer Care Bot/)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Import Tenant" }));

    await within(dialog).findByText("Tenant imported successfully");
    // The exact parsed package is sent — ids are never rewritten client-side.
    expect(api.importTenantPackage).toHaveBeenCalledWith(PACKAGE);
    expect(within(dialog).getByText(/Bots imported/).textContent).toContain("3");
    // The tenant list reloads so the imported tenant is immediately visible.
    await waitFor(() => expect(api.listTenants).toHaveBeenCalledTimes(2));
  });

  it("rejects a file that is not valid JSON without calling the API", async () => {
    const user = userEvent.setup();
    const dialog = await openImportDialog(user);

    await user.upload(
      within(dialog).getByLabelText("Choose tenant JSON file"),
      packageFile("definitely-not-json{{", "broken.json"),
    );

    await within(dialog).findByText(/not valid JSON/);
    expect(within(dialog).getByRole("button", { name: "Import Tenant" })).toBeDisabled();
    expect(api.importTenantPackage).not.toHaveBeenCalled();
  });

  it("rejects JSON that is not a tenant export package", async () => {
    const user = userEvent.setup();
    const dialog = await openImportDialog(user);

    await user.upload(
      within(dialog).getByLabelText("Choose tenant JSON file"),
      packageFile(JSON.stringify({ nodes: [], edges: [] }), "flow.workflow.json"),
    );

    await within(dialog).findByText(/not a tenant export package/);
    expect(within(dialog).getByRole("button", { name: "Import Tenant" })).toBeDisabled();
  });

  it("shows the backend's exact message on an id collision (409)", async () => {
    vi.mocked(api.importTenantPackage).mockRejectedValue(new Error(
      "Import collision: bot 'bot_71194477c0eb' already exists and belongs to tenant 'tn_other'.",
    ));
    const user = userEvent.setup();
    const dialog = await openImportDialog(user);

    await user.upload(
      within(dialog).getByLabelText("Choose tenant JSON file"),
      packageFile(JSON.stringify(PACKAGE)),
    );
    await within(dialog).findByText("Honasa Care — ready to import");
    await user.click(within(dialog).getByRole("button", { name: "Import Tenant" }));

    await within(dialog).findByText(
      "Import collision: bot 'bot_71194477c0eb' already exists and belongs to tenant 'tn_other'.",
    );
    // The dialog stays open so the operator can fix the package and retry.
    expect(within(dialog).getByRole("button", { name: "Import Tenant" })).toBeEnabled();
    expect(api.listTenants).toHaveBeenCalledTimes(1);
  });
});
