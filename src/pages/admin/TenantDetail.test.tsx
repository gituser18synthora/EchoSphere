import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TenantDetail from "@/pages/admin/TenantDetail";
import * as api from "@/services/api";

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
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), hasPermission: () => false }),
}));

const TENANT = {
  id: "tn-voice", name: "Voice Corp", code: "voice", domain: "voice.example",
  industry: "finance", region: "in-mumbai", aiProfileCode: "balanced",
  defaultLanguages: ["en-IN"], plan: "growth", status: "active",
  createdAt: "2026-01-01T00:00:00Z", users: 1, bots: 1, callsMonth: 0,
  minutesMonth: 0, mrr: 0, aiCostMonth: 0, health: "good",
  adminEmail: "admin@voice.example",
};

describe("TenantDetail — assigned languages", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getTenant).mockResolvedValue(TENANT as never);
    vi.mocked(api.getTenantAnalytics).mockResolvedValue({ callsSeries: [] } as never);
    vi.mocked(api.getOnboardingOptions).mockResolvedValue({
      industries: [{ code: "finance", name: "Finance" }],
      dataRegions: [{ code: "in-mumbai", name: "India", infrastructureReady: true }],
      plans: [{ code: "growth", name: "Growth" }],
      aiProfiles: [{ code: "balanced", name: "Balanced" }],
      languages: [
        { code: "en-IN", name: "English (India)", nativeName: "English" },
        { code: "hi-IN", name: "Hindi", nativeName: "हिन्दी" },
      ],
    } as never);
    vi.mocked(api.updateTenant).mockResolvedValue(TENANT as never);
  });

  it("lets a Super Admin edit the onboarding language assignment", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/admin/tenants/tn-voice"]}>
        <Routes><Route path="/admin/tenants/:tenantId" element={<TenantDetail />} /></Routes>
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole("button", { name: "Edit tenant" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit tenant" });
    const languages = within(dialog).getByRole("group", { name: "Assigned languages" });
    expect(within(languages).getByRole("button", { name: "English (India) (en-IN)" })).toHaveAttribute("aria-pressed", "true");

    await user.click(within(languages).getByRole("button", { name: "Hindi (hi-IN)" }));
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(api.updateTenant).toHaveBeenCalledWith("tn-voice", {
        defaultLanguages: ["en-IN", "hi-IN"],
      });
    });
  });
});
