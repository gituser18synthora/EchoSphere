import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Onboarding from "./Onboarding";
import * as api from "@/services/api";

vi.mock("@/services/api", () => ({
  getOnboardingOptions: vi.fn(),
  createTenant: vi.fn(),
  saveTenantSettings: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast: vi.fn(), hasPermission: () => true }),
}));

const OPTIONS = {
  industries: [
    { code: "healthcare", name: "Healthcare", icon: "", defaultGuardrailProfileId: "gp_health" },
    { code: "banking", name: "Banking", icon: "", defaultGuardrailProfileId: "gp_fin" },
    { code: "ecommerce", name: "E-commerce", icon: "", defaultGuardrailProfileId: "gp_std" },
  ],
  dataRegions: [{ code: "in", name: "India", infrastructureReady: true }],
  plans: [{
    code: "starter", name: "Starter", description: "Starter plan", priceMonthly: 490,
    minutesIncluded: 10000, botLimit: 2, seatsIncluded: 5, isRecommended: true,
  }],
  aiProfiles: [{ code: "balanced", name: "Balanced", description: "Default", costCategory: "medium" }],
  languages: [{ code: "en-IN", name: "English (India)", nativeName: "English", direction: "ltr" }],
  guardrailProfiles: [
    { id: "gp_std", code: "standard", name: "Standard", description: "Baseline safety" },
    { id: "gp_health", code: "healthcare", name: "Healthcare", description: "Adds medical boundary" },
    { id: "gp_fin", code: "finance", name: "Finance", description: "Adds payment restrictions" },
  ],
};

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={["/admin/onboarding"]}>
      <Onboarding />
    </MemoryRouter>,
  );

const continueButton = () => screen.getByRole("button", { name: /continue/i });

/** Fill the required fields of steps 0–2 and land on AI Configuration. */
async function goToAiConfiguration(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByPlaceholderText("Grove Utilities Inc."), "Acme Care");
  await user.type(screen.getByPlaceholderText("groveutilities.com"), "acme.com");
  await user.click(continueButton()); // → Subscription
  await user.click(continueButton()); // → Admin User
  await user.type(screen.getByPlaceholderText("Jordan Fisher"), "Jordan Fisher");
  await user.type(screen.getByPlaceholderText("admin@acme.com"), "admin@acme.com");
  await user.click(continueButton()); // → AI Configuration
  return screen.findByRole("combobox", { name: /guardrail profile/i }) as Promise<HTMLSelectElement>;
}

describe("Onboarding — guardrail profiles", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getOnboardingOptions).mockResolvedValue(OPTIONS as never);
    vi.mocked(api.createTenant).mockResolvedValue({ id: "tn_new" } as never);
    vi.mocked(api.saveTenantSettings).mockResolvedValue({} as never);
  });

  it("preselects the industry's default profile (Healthcare → Healthcare)", async () => {
    const user = userEvent.setup();
    renderPage();
    // Healthcare is the first industry, so it is the default selection.
    const guardrail = await goToAiConfiguration(user);
    expect(guardrail.value).toBe("gp_health");
  });

  it("suggests the Finance profile when a finance industry is selected", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.selectOptions(
      await screen.findByRole("combobox", { name: /^industry/i }), "banking");
    const guardrail = await goToAiConfiguration(user);
    expect(guardrail.value).toBe("gp_fin");
  });

  it("keeps a manual override when the industry changes afterwards", async () => {
    const user = userEvent.setup();
    renderPage();
    const guardrail = await goToAiConfiguration(user);
    await user.selectOptions(guardrail, "gp_std"); // manual override

    // Unrelated edits on this step never reset the selection.
    await user.click(screen.getByTitle("English")); // toggle the language chip
    expect((screen.getByRole("combobox", { name: /guardrail profile/i }) as HTMLSelectElement).value).toBe("gp_std");

    // Walk back to Company and change the industry — the override sticks.
    const back = () => screen.getByRole("button", { name: /back/i });
    await user.click(back());
    await user.click(back());
    await user.click(back());
    await user.selectOptions(screen.getByRole("combobox", { name: /^industry/i }), "banking");
    await user.click(continueButton());
    await user.click(continueButton());
    await user.click(continueButton());
    const after = await screen.findByRole("combobox", { name: /guardrail profile/i }) as HTMLSelectElement;
    expect(after.value).toBe("gp_std");
  });

  it("sends the selected profile on tenant creation", async () => {
    const user = userEvent.setup();
    renderPage();
    const guardrail = await goToAiConfiguration(user);
    await user.selectOptions(guardrail, "gp_fin"); // Super Admin override
    await user.click(continueButton()); // → Telephony
    await user.click(continueButton()); // → Security
    await user.click(continueButton()); // → Review & Launch
    await user.click(screen.getByRole("button", { name: /launch provisioning/i }));

    await waitFor(() => {
      expect(api.createTenant).toHaveBeenCalledWith(
        expect.objectContaining({
          industry: "healthcare",
          guardrailProfileId: "gp_fin",
        }),
      );
    });
  });
});
