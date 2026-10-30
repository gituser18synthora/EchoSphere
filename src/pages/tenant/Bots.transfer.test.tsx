/* Bot Export / Import on the My VoiceBots page.

   - "Export bot JSON" in the bot menu downloads bot_<id>.json via the real
     export API (bots.manage only);
   - "Import bot" opens a modal: choosing the file parses it, asks the backend
     for a dry-run preview (tenant id, bot id, name, existing yes/no, action,
     resources changed, warnings), makes the update case explicit, and only
     then calls the import API with the current tenant; the list refreshes;
   - parse and backend errors (wrong package kind, tenant mismatch) surface
     their exact message and never reach the import call. */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Bots from "@/pages/tenant/Bots";
import * as api from "@/services/api";
import type { VoiceBot } from "@/types/domain";

vi.mock("@/services/api", () => ({
  listBots: vi.fn(),
  createBot: vi.fn(),
  cloneBot: vi.fn(),
  listLanguages: vi.fn(),
  simulateAction: vi.fn(),
  archiveBot: vi.fn(),
  restoreBot: vi.fn(),
  deleteBot: vi.fn(),
  exportBotPackage: vi.fn(),
  previewBotImport: vi.fn(),
  importBotPackage: vi.fn(),
}));

const toast = vi.fn();
let grantedPermissions = new Set<string>(["bots.manage", "costs.view"]);
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({
    toast,
    hasPermission: (code: string) => grantedPermissions.has(code),
    user: { id: "usr_1", tenantId: "tn_a" },
  }),
}));

const listBots = vi.mocked(api.listBots);
const exportBotPackage = vi.mocked(api.exportBotPackage);
const previewBotImport = vi.mocked(api.previewBotImport);
const importBotPackage = vi.mocked(api.importBotPackage);

const BOT: VoiceBot = {
  id: "bot_abc123",
  tenantId: "tn_a",
  name: "Order Assistant",
  useCase: "Order status",
  description: "",
  languages: ["hi-IN"],
  status: "published",
  version: "v1.2.0",
  liveVersion: "v1.2.0",
  owner: "Asha",
  health: "good",
  containment: 70,
  callsToday: 3,
  callsMonth: 120,
  avgCostPerCall: 0.4,
  csat: 4.5,
  channels: ["voice"],
  guardrailProfileId: "",
  updatedAt: "2026-09-10T10:00:00Z",
  readiness: [],
};

const PACKAGE: api.BotExportPackage = {
  kind: "echosphere.bot.export",
  schema_version: 1,
  exported_at: "2026-09-11T08:00:00+00:00",
  tenant_id: "tn_a",
  bot_id: "bot_abc123",
  source: { tenant_name: "Acme", bot_name: "Order Assistant", bot_status: "published" },
  bot: { id: "bot_abc123", tenant_id: "tn_a", name: "Order Assistant", status: "published", languages: ["hi-IN"] },
  resources: {
    workflows: [{ id: "wf_1" }],
    prompts: [{ id: "pr_1", versions: [{ id: "prv_1" }, { id: "prv_2" }] }],
    intents: [{ id: "in_1" }, { id: "in_2" }],
    api_connections: [{ id: "api_1" }],
    knowledge_sources: [{ id: "ks_1" }],
    test_scenarios: [],
    releases: [{ id: "rel_1" }],
  },
  shared: { api_connections: [{ id: "api_shared" }], entity_defs: [] },
  environment: { channel_configs: [{ id: "ch_1", type: "voice" }], phone_numbers: [{ number: "+14155550101" }] },
  knowledge_plane: { documents: [{ id: "kdoc_1" }] },
  integrity: "sha256:abc",
};

const PREVIEW: api.BotImportReport = {
  botId: "bot_abc123",
  tenantId: "tn_a",
  botName: "Order Assistant",
  existing: true,
  action: "update",
  dryRun: true,
  created: { intent: 1 },
  updated: { bot: 1, workflow: 1, prompt: 1 },
  removed: { prompt: 1 },
  reused: { tenant_api_connection: 1 },
  remappedIds: {},
  preserved: [{ kind: "channel", label: "voice", reason: "kept this environment's channel configuration", differs: ["phoneNumber"] }],
  secretsMissing: [{ owner: "API connection 'Fetch order'", reference: "secret://orders-api-key" }],
  warnings: ["channel 'voice' kept its configuration on this environment (package differs in: phoneNumber)."],
  knowledgeDocuments: 1,
};

function packageFile(content: string, name = "bot_bot_abc123.json"): File {
  return new File([content], name, { type: "application/json" });
}

async function openImportModal() {
  render(<MemoryRouter><Bots /></MemoryRouter>);
  await screen.findByText("Order Assistant");
  await userEvent.click(screen.getByRole("button", { name: /import bot/i }));
  return screen.getByRole("dialog", { name: "Import bot" });
}

beforeEach(() => {
  vi.clearAllMocks();
  grantedPermissions = new Set(["bots.manage", "costs.view"]);
  listBots.mockResolvedValue([BOT]);
  vi.mocked(api.listLanguages).mockResolvedValue([]);
  URL.createObjectURL = vi.fn(() => "blob:bot-package");
  URL.revokeObjectURL = vi.fn();
  HTMLAnchorElement.prototype.click = vi.fn();
  previewBotImport.mockResolvedValue(PREVIEW);
  importBotPackage.mockResolvedValue({ ...PREVIEW, dryRun: false });
});

describe("Export bot JSON", () => {
  it("downloads the package from the export API and confirms with a toast", async () => {
    exportBotPackage.mockResolvedValue(PACKAGE);
    render(<MemoryRouter><Bots /></MemoryRouter>);
    await screen.findByText("Order Assistant");
    await userEvent.click(screen.getByRole("button", { name: "More actions" }));
    await userEvent.click(screen.getByRole("menuitem", { name: /export bot json/i }));
    await waitFor(() => expect(exportBotPackage).toHaveBeenCalledWith("bot_abc123"));
    await waitFor(() => expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled());
    expect(toast).toHaveBeenCalledWith(expect.stringContaining("bot_bot_abc123.json"));
  });

  it("surfaces the API error", async () => {
    exportBotPackage.mockRejectedValue(new Error("VoiceBot not found."));
    render(<MemoryRouter><Bots /></MemoryRouter>);
    await screen.findByText("Order Assistant");
    await userEvent.click(screen.getByRole("button", { name: "More actions" }));
    await userEvent.click(screen.getByRole("menuitem", { name: /export bot json/i }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("VoiceBot not found.", "error"));
  });

  it("is hidden without bots.manage, as is Import bot", async () => {
    grantedPermissions = new Set(["costs.view"]);
    render(<MemoryRouter><Bots /></MemoryRouter>);
    await screen.findByText("Order Assistant");
    expect(screen.queryByRole("button", { name: /import bot/i })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "More actions" }));
    expect(screen.queryByRole("menuitem", { name: /export bot json/i })).toBeNull();
  });
});

describe("Import bot", () => {
  it("previews the package against the current tenant, shows the update summary, then imports", async () => {
    const dialog = await openImportModal();
    const input = within(dialog).getByLabelText("Choose bot JSON file");
    await userEvent.upload(input, packageFile(JSON.stringify(PACKAGE)));

    await waitFor(() => expect(previewBotImport).toHaveBeenCalledWith(
      expect.objectContaining({ bot_id: "bot_abc123" }),
      { tenantId: "tn_a", applyEnvironment: false },
    ));
    expect(importBotPackage).not.toHaveBeenCalled();

    await within(dialog).findByText(/existing bot will be updated: order assistant/i);
    expect(within(dialog).getByText("tn_a")).toBeInTheDocument();
    expect(within(dialog).getByText("bot_abc123")).toBeInTheDocument();
    expect(within(dialog).getByText("Yes")).toBeInTheDocument(); // Existing bot
    expect(within(dialog).getByText("Update")).toBeInTheDocument(); // Action
    expect(within(dialog).getByText(/will be removed/i).parentElement?.textContent).toMatch(/1 prompts/);
    expect(within(dialog).getByText(/secret:\/\/orders-api-key/)).toBeInTheDocument();
    expect(within(dialog).getByText(/package differs in phoneNumber/)).toBeInTheDocument();
    expect(within(dialog).getByText(/1 workflows · 1 prompts · 2 prompt versions · 2 intents/)).toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole("button", { name: /update existing bot/i }));
    await waitFor(() => expect(importBotPackage).toHaveBeenCalledWith(
      expect.objectContaining({ bot_id: "bot_abc123", integrity: "sha256:abc" }),
      { tenantId: "tn_a", applyEnvironment: false },
    ));
    await within(dialog).findByText("Bot updated");
    expect(toast).toHaveBeenCalledWith(expect.stringContaining("updated from the imported package"));
    expect(listBots).toHaveBeenCalledTimes(2); // initial load + reload after import
  });

  it("offers Create when the bot does not exist yet and passes the environment toggle", async () => {
    previewBotImport.mockResolvedValue({ ...PREVIEW, existing: false, action: "create", updated: {}, removed: {} });
    const dialog = await openImportModal();
    await userEvent.upload(within(dialog).getByLabelText("Choose bot JSON file"), packageFile(JSON.stringify(PACKAGE)));
    await within(dialog).findByText(/new bot will be created: order assistant/i);
    expect(within(dialog).getByText("No")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("switch"));
    await waitFor(() => expect(previewBotImport).toHaveBeenLastCalledWith(
      expect.anything(), { tenantId: "tn_a", applyEnvironment: true },
    ));
    await userEvent.click(within(dialog).getByRole("button", { name: /^create bot$/i }));
    await waitFor(() => expect(importBotPackage).toHaveBeenCalledWith(
      expect.anything(), { tenantId: "tn_a", applyEnvironment: true },
    ));
  });

  it("rejects a tenant package file before calling the backend", async () => {
    const dialog = await openImportModal();
    await userEvent.upload(
      within(dialog).getByLabelText("Choose bot JSON file"),
      packageFile(JSON.stringify({ kind: "echosphere.tenant.export", schema_version: 1, resources: { tenant: { id: "tn_a" } } })),
    );
    await within(dialog).findByText(/not a bot export package/i);
    expect(within(dialog).getByText(/Admin → Organizations/)).toBeInTheDocument();
    expect(previewBotImport).not.toHaveBeenCalled();
    expect(within(dialog).queryByRole("button", { name: /update existing bot|create bot/i })).toBeDisabled();
  });

  it("shows the backend's tenant-mismatch error from the preview and blocks import", async () => {
    previewBotImport.mockRejectedValue(new Error(
      "This package belongs to tenant 'tn_b' but the destination tenant is 'tn_a'. A bot can only be imported into the tenant it was exported from.",
    ));
    const dialog = await openImportModal();
    await userEvent.upload(within(dialog).getByLabelText("Choose bot JSON file"), packageFile(JSON.stringify({ ...PACKAGE, tenant_id: "tn_b" })));
    await within(dialog).findByText(/belongs to tenant 'tn_b'/);
    expect(within(dialog).getByRole("button", { name: /create bot/i })).toBeDisabled();
    expect(importBotPackage).not.toHaveBeenCalled();
  });

  it("surfaces an import failure and keeps the modal open", async () => {
    importBotPackage.mockRejectedValue(new Error("Import collision: workflow 'wf_1' already exists and belongs to bot 'bot_other'."));
    const dialog = await openImportModal();
    await userEvent.upload(within(dialog).getByLabelText("Choose bot JSON file"), packageFile(JSON.stringify(PACKAGE)));
    await within(dialog).findByText(/existing bot will be updated/i);
    await userEvent.click(within(dialog).getByRole("button", { name: /update existing bot/i }));
    await within(dialog).findByText(/Import collision: workflow 'wf_1'/);
    expect(toast).not.toHaveBeenCalledWith(expect.stringContaining("updated from the imported package"));
  });
});
