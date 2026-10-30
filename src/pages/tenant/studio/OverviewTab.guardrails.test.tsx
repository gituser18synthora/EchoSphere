import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import OverviewTab from "./OverviewTab";
import * as api from "@/services/api";

const hasPermissionMock = vi.fn(() => true);
vi.mock("@/services/api", () => ({
  listAudit: vi.fn(),
  listLanguages: vi.fn(),
  updateBot: vi.fn(),
  getBotEffectiveGuardrails: vi.fn(),
  listGuardrailProfiles: vi.fn(),
  setBotGuardrailProfile: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), hasPermission: hasPermissionMock }),
}));

const BOT = {
  id: "bot_x", tenantId: "tn_x", name: "Collections Bot", useCase: "Collections",
  description: "Collects", languages: ["hi-IN"], status: "published",
  version: "v1", liveVersion: "v1", owner: "Owner", health: "neutral",
  containment: 0, callsToday: 0, callsMonth: 0, avgCostPerCall: 0, csat: 0,
  channels: [], voiceId: "", guardrailProfileId: "", updatedAt: "2026-08-10",
  readiness: [],
};

const FINANCE = { id: "gp_fin", code: "finance", name: "Finance", status: "active", version: 1 };
const DEV = { id: "gp_dev", code: "development", name: "Development / Internal", status: "active", version: 1 };

const INHERITED = {
  botId: "bot_x", tenantId: "tn_x", inherited: true,
  profile: FINANCE, tenantDefaultProfile: FINANCE,
  rules: [
    { guardrailId: "g1", code: "pii_redaction", name: "PII redaction", category: "Privacy", action: "redact", mandatory: true },
    { guardrailId: "g2", code: "payment_collection_restriction", name: "Payment collection restriction", category: "Compliance", action: "block", mandatory: false },
  ],
  compliancePolicies: [
    { code: "internal_collections_waiver", version: 1, name: "Waiver authorization", regulator: "internal", jurisdiction: "IN", timezone: "Asia/Kolkata", callingWindows: [] },
  ],
  degraded: false,
};

const PROFILES = [
  { ...FINANCE, description: "", usageCount: 1, guardrailIds: [], guardrails: [], createdAt: "", updatedAt: "", createdBy: "", updatedBy: "" },
  { ...DEV, description: "", usageCount: 0, guardrailIds: [], guardrails: [], createdAt: "", updatedAt: "", createdBy: "", updatedBy: "" },
];

const renderTab = () =>
  render(
    <MemoryRouter>
      <OverviewTab bot={BOT as never} />
    </MemoryRouter>,
  );

describe("Bot guardrails panel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    hasPermissionMock.mockReturnValue(true);
    vi.mocked(api.listAudit).mockResolvedValue([]);
    vi.mocked(api.getBotEffectiveGuardrails).mockResolvedValue(INHERITED as never);
    vi.mocked(api.listGuardrailProfiles).mockResolvedValue(PROFILES as never);
    vi.mocked(api.setBotGuardrailProfile).mockResolvedValue({} as never);
  });

  it("shows the inherited tenant default with the effective rules and policy versions", async () => {
    renderTab();
    expect(await screen.findByText("Inherited")).toBeInTheDocument();
    const select = await screen.findByRole("combobox", { name: "Guardrail profile" });
    expect((select as HTMLSelectElement).value).toBe("");
    expect(screen.getByText("Inherit tenant default — Finance")).toBeInTheDocument();
    expect(screen.getByText("PII redaction")).toBeInTheDocument();
    expect(screen.getByText("Mandatory")).toBeInTheDocument();
    expect(screen.getByText(/internal_collections_waiver v1/)).toBeInTheDocument();
  });

  it("assigns an explicit profile through the API", async () => {
    const user = userEvent.setup();
    renderTab();
    const select = await screen.findByRole("combobox", { name: "Guardrail profile" });
    await user.selectOptions(select, "gp_dev");
    await waitFor(() => {
      expect(api.setBotGuardrailProfile).toHaveBeenCalledWith("bot_x", "gp_dev");
    });
  });

  it("shows an explicit assignment as pinned against tenant-default changes", async () => {
    vi.mocked(api.getBotEffectiveGuardrails).mockResolvedValue({
      ...INHERITED, inherited: false, profile: DEV,
    } as never);
    renderTab();
    expect(await screen.findByText("Explicit")).toBeInTheDocument();
    expect(screen.getByText(/Tenant-default changes do not affect this bot/)).toBeInTheDocument();
  });

  it("hides the selector without governance permission but keeps the view", async () => {
    hasPermissionMock.mockReturnValue(false);
    renderTab();
    expect(await screen.findByText("Inherited")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Guardrail profile" })).toBeNull();
    expect(screen.getByText("PII redaction")).toBeInTheDocument();
  });
});
