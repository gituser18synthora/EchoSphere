/* Team page member-adding flow: the role dropdown offers ONLY Tenant User
   (admin/platform roles are never exposed here — the backend rejects them
   too), and the modal supports both an email invite and direct creation with
   a password. The role sent to the API is always tenant_user. */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Team from "@/pages/tenant/Team";
import * as api from "@/services/api";

vi.mock("@/services/api", () => ({
  listTeam: vi.fn(),
  listRoles: vi.fn(),
  inviteUser: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({
    user: { tenantName: "OYO", tenantId: "tn_oyo" },
    toast: vi.fn(),
    hasPermission: () => true,
  }),
}));

const ROLES = [
  { id: "r1", code: "tenant_admin", name: "Tenant Admin", description: "", scope: "tenant", permissions: ["team.manage"], permissionCount: 1, members: 1 },
  { id: "r2", code: "tenant_user", name: "Tenant User", description: "", scope: "tenant", permissions: ["bots.view"], permissionCount: 1, members: 1 },
];

async function openModal() {
  const user = userEvent.setup();
  render(<Team />);
  await user.click(await screen.findByRole("button", { name: "Invite / Create Member" }));
  return { user, dialog: await screen.findByRole("dialog") };
}

describe("Team — add member", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listTeam).mockResolvedValue([]);
    vi.mocked(api.listRoles).mockResolvedValue(ROLES as never);
    vi.mocked(api.inviteUser).mockResolvedValue({ id: "u1", temporaryPassword: "tmp" } as never);
  });

  it("offers only the Tenant User role", async () => {
    const { dialog } = await openModal();
    const select = within(dialog).getByRole("combobox", { name: "Role" });
    const options = within(select).getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(["Tenant User"]);
  });

  it("invite flow posts tenant_user with no password", async () => {
    const { user, dialog } = await openModal();
    await user.type(within(dialog).getByPlaceholderText("Full name"), "Asha Rao");
    await user.type(within(dialog).getByPlaceholderText("colleague@company.com"), "asha@oyo.com");
    await user.click(within(dialog).getByRole("button", { name: "Send invite" }));
    expect(api.inviteUser).toHaveBeenCalledWith({
      name: "Asha Rao", email: "asha@oyo.com", roleCode: "tenant_user",
    });
  });

  it("create flow posts tenant_user with the chosen password", async () => {
    const { user, dialog } = await openModal();
    await user.click(within(dialog).getByRole("button", { name: "Create user" }));
    await user.type(within(dialog).getByPlaceholderText("Full name"), "Ravi Iyer");
    await user.type(within(dialog).getByPlaceholderText("colleague@company.com"), "ravi@oyo.com");
    const [pw, confirm] = within(dialog).getAllByLabelText(/password/i);
    await user.type(pw, "Direct2026pw");
    await user.type(confirm, "Direct2026pw");
    // Footer submit button carries the same "Create user" label as the mode
    // toggle; the footer one is the last in DOM order.
    const buttons = within(dialog).getAllByRole("button", { name: "Create user" });
    await user.click(buttons[buttons.length - 1]);
    expect(api.inviteUser).toHaveBeenCalledWith({
      name: "Ravi Iyer", email: "ravi@oyo.com", roleCode: "tenant_user",
      password: "Direct2026pw",
    });
  });

  it("rejects mismatched passwords before calling the API", async () => {
    const { user, dialog } = await openModal();
    await user.click(within(dialog).getByRole("button", { name: "Create user" }));
    await user.type(within(dialog).getByPlaceholderText("Full name"), "Mismatch");
    await user.type(within(dialog).getByPlaceholderText("colleague@company.com"), "m@oyo.com");
    const [pw, confirm] = within(dialog).getAllByLabelText(/password/i);
    await user.type(pw, "Direct2026pw");
    await user.type(confirm, "Different2026pw");
    const buttons = within(dialog).getAllByRole("button", { name: "Create user" });
    await user.click(buttons[buttons.length - 1]);
    expect(api.inviteUser).not.toHaveBeenCalled();
    expect(within(dialog).getByText("Passwords do not match")).toBeInTheDocument();
  });
});
