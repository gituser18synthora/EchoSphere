/* Team page member-adding flow: the role dropdown offers ONLY Tenant User
   (admin/platform roles are never exposed here — the backend rejects them
   too), and the modal supports both an email invite and direct creation with
   a password. The role sent to the API is always tenant_user.

   Change-password flow: each OTHER member's row menu offers "Change password"
   (never the signed-in admin's own row); the modal validates policy + match
   client-side and posts newPassword/confirmPassword to the reset API. */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Team from "@/pages/tenant/Team";
import * as api from "@/services/api";

vi.mock("@/services/api", () => ({
  listTeam: vi.fn(),
  listRoles: vi.fn(),
  inviteUser: vi.fn(),
  resetUserPassword: vi.fn(),
}));
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({
    user: { id: "usr_admin", email: "admin@oyo.com", tenantName: "OYO", tenantId: "tn_oyo" },
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

const MEMBERS = [
  { id: "usr_admin", name: "Priya Admin", email: "admin@oyo.com", role: "Tenant Admin", roleCode: "tenant_admin", status: "active", lastActive: "—", botsOwned: 0 },
  { id: "usr_member", name: "Ravi Member", email: "ravi@oyo.com", role: "Tenant User", roleCode: "tenant_user", status: "active", lastActive: "—", botsOwned: 1 },
];

describe("Team — change member password", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listTeam).mockResolvedValue(MEMBERS as never);
    vi.mocked(api.listRoles).mockResolvedValue(ROLES as never);
    vi.mocked(api.resetUserPassword).mockResolvedValue(
      { reset: true, sessionsInvalidated: true } as never,
    );
  });

  /** Opens the change-password modal for the second row (the non-self member). */
  async function openChangePassword() {
    const user = userEvent.setup();
    render(<Team />);
    const menus = await screen.findAllByRole("button", { name: "More actions" });
    await user.click(menus[1]);
    await user.click(await screen.findByRole("menuitem", { name: "Change password" }));
    return { user, dialog: await screen.findByRole("dialog") };
  }

  it("never offers changing the signed-in admin's own password", async () => {
    const user = userEvent.setup();
    render(<Team />);
    const menus = await screen.findAllByRole("button", { name: "More actions" });
    await user.click(menus[0]); // own row (usr_admin)
    expect(await screen.findByRole("menuitem", { name: "Change role" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Change password" })).not.toBeInTheDocument();
  });

  it("submits the new password for the selected member with show/hide fields", async () => {
    const { user, dialog } = await openChangePassword();
    // Both fields are masked with a visibility toggle; the current password is
    // neither shown nor asked for.
    expect(within(dialog).getAllByRole("button", { name: "Show password" })).toHaveLength(2);
    expect(within(dialog).queryByLabelText(/current password/i)).not.toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("New password"), "Rotate2026pw");
    await user.type(within(dialog).getByLabelText("Confirm password"), "Rotate2026pw");
    await user.click(within(dialog).getByRole("button", { name: "Change password" }));
    expect(api.resetUserPassword).toHaveBeenCalledWith("usr_member", {
      newPassword: "Rotate2026pw", confirmPassword: "Rotate2026pw",
    });
  });

  it("rejects mismatched passwords before calling the API", async () => {
    const { user, dialog } = await openChangePassword();
    await user.type(within(dialog).getByLabelText("New password"), "Rotate2026pw");
    await user.type(within(dialog).getByLabelText("Confirm password"), "Different2026pw");
    await user.click(within(dialog).getByRole("button", { name: "Change password" }));
    expect(api.resetUserPassword).not.toHaveBeenCalled();
    expect(within(dialog).getByText("Passwords do not match")).toBeInTheDocument();
  });

  it("enforces the shared password policy client-side", async () => {
    const { user, dialog } = await openChangePassword();
    await user.type(within(dialog).getByLabelText("New password"), "alllowercase1");
    await user.type(within(dialog).getByLabelText("Confirm password"), "alllowercase1");
    await user.click(within(dialog).getByRole("button", { name: "Change password" }));
    expect(api.resetUserPassword).not.toHaveBeenCalled();
    expect(within(dialog).getByText(/an uppercase letter/)).toBeInTheDocument();
  });
});
