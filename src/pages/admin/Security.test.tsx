import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Security from "./Security";
import * as api from "@/services/api";
vi.mock("@/services/api", () => ({ listSecurityUsers: vi.fn(), listRoles: vi.fn(), listPermissions: vi.fn(), listAllTenants: vi.fn(),
  inviteUser: vi.fn(), updateUser: vi.fn(), resetUserPassword: vi.fn(), saveRolePermissions: vi.fn(),
  listAuditPage: vi.fn(), getAuditEvent: vi.fn(), downloadAuditExport: vi.fn() }));
vi.mock("@/state/AppContext", () => ({ useApp: () => ({ user: { id: "root", role: "super_admin" }, toast: vi.fn() }) }));
const roles = [
  { id: "r_super", code: "super_admin", name: "Super Admin", scope: "platform", permissions: ["security.manage"], permissionCount: 1, members: 1 },
  { id: "r_user", code: "tenant_user", name: "Tenant User", scope: "tenant", permissions: ["manage_voices"], permissionCount: 1, members: 5 },
];
const member = { id: "member", name: "Team Member", email: "member@example.com", role: "Tenant User", roleCode: "tenant_user", status: "active", tenantId: "tn_a", tenantName: "Acme" };
const event = { id: "event", actor: "Admin", actorRole: "super_admin", action: "Changed voice", target: "Local bot", time: "2026-11-13T10:00:00Z", ip: "127.0.0.1", tenant: "Acme" };
const page = <T,>(items: T[], total = items.length) => ({ items, meta: { page: 1, pageSize: 25, total, totalPages: Math.max(1, Math.ceil(total / 25)) } });
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.listSecurityUsers).mockResolvedValue(page([member]) as never);
  vi.mocked(api.listRoles).mockResolvedValue(roles as never);
  vi.mocked(api.listAllTenants).mockResolvedValue([{ id: "tn_a", name: "Acme" }] as never);
  vi.mocked(api.listPermissions).mockResolvedValue([
    { id: "p1", code: "manage_voices", name: "Manage voice configuration", category: "tenant", description: "Edit voice engines" },
    { id: "p2", code: "manage_workflows", name: "Manage workflows", category: "tenant", description: "Edit conversation workflows" },
    { id: "p3", code: "security.manage", name: "Manage platform security", category: "platform", description: "Platform only" },
  ]);
  vi.mocked(api.updateUser).mockResolvedValue(member as never);
  vi.mocked(api.inviteUser).mockResolvedValue({ ...member, temporaryPassword: "TemporaryTest9!" } as never);
  vi.mocked(api.saveRolePermissions).mockResolvedValue(roles[1] as never);
  vi.mocked(api.listAuditPage).mockResolvedValue(page([event], 60) as never);
  vi.mocked(api.getAuditEvent).mockResolvedValue({ ...event, previousValue: { enabled: false }, newValue: { enabled: true } } as never);
  vi.mocked(api.downloadAuditExport).mockResolvedValue("echosphere-audit.csv");
});

describe("Security management", () => {
  it("loads audit history only when its tab opens, pages it on the server, and opens change details", async () => {
    const user = userEvent.setup(); render(<Security />);
    await screen.findByText("Team Member");
    expect(api.listAuditPage).not.toHaveBeenCalled();
    await user.click(screen.getByRole("tab", { name: "Audit Logs" }));
    await screen.findByText("Changed voice");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(api.listAuditPage).toHaveBeenLastCalledWith(expect.objectContaining({ page: 2 })));
    await user.click(await screen.findByText("Changed voice"));
    const dialog = await screen.findByRole("dialog", { name: "Audit event" });
    expect(await within(dialog).findByText(/"enabled": true/)).toBeInTheDocument();
    expect(api.getAuditEvent).toHaveBeenCalledWith("event");
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("button", { name: "Export filtered CSV" }));
    expect(api.downloadAuditExport).toHaveBeenCalledWith(expect.objectContaining({ page: 2 }));
  });

  it("requires a review before applying an existing tenant permission", async () => {
    const user = userEvent.setup(); render(<Security />);
    await user.click(screen.getByRole("tab", { name: "Roles" }));
    await user.click(await screen.findByRole("button", { name: "Manage permissions" }));
    const dialog = await screen.findByRole("dialog", { name: "Manage permissions: Tenant User" });
    expect(within(dialog).queryByText("Manage platform security")).not.toBeInTheDocument();
    expect(within(dialog).getByText(/all 5 members/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("checkbox", { name: /Manage workflows/ }));
    await user.click(within(dialog).getByRole("button", { name: "Review changes" }));
    expect(api.saveRolePermissions).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Confirm changes" }));
    await waitFor(() => expect(api.saveRolePermissions).toHaveBeenCalledWith("r_user", ["manage_voices", "manage_workflows"], ["manage_voices"]));
  });

  it("retains the permission selection if the server reports a concurrent edit", async () => {
    vi.mocked(api.saveRolePermissions).mockRejectedValue(Object.assign(new Error("Permissions changed. Reload before saving."), { status: 409 }));
    const user = userEvent.setup(); render(<Security />);
    await user.click(screen.getByRole("tab", { name: "Roles" }));
    await user.click(await screen.findByRole("button", { name: "Manage permissions" }));
    const dialog = screen.getByRole("dialog", { name: "Manage permissions: Tenant User" });
    await user.click(within(dialog).getByRole("checkbox", { name: /Manage workflows/ }));
    await user.click(within(dialog).getByRole("button", { name: "Review changes" }));
    await user.click(within(dialog).getByRole("button", { name: "Confirm changes" }));
    expect(await within(dialog).findByRole("button", { name: "Reload latest permissions" })).toBeInTheDocument();
    expect(within(dialog).getByText("Added (1)")).toBeInTheDocument();
  });

  it("reuses user creation with organization and role, showing the temporary password only after saving", async () => {
    const user = userEvent.setup(); render(<Security />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Create user" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Create user" }));
    const dialog = screen.getByRole("dialog", { name: "Create user" });
    await user.type(within(dialog).getByRole("textbox", { name: /^Name/ }), "New Member");
    await user.type(within(dialog).getByRole("textbox", { name: /^Email/ }), "new@example.com");
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Organization" }), "tn_a");
    await user.click(within(dialog).getByRole("button", { name: "Review changes" }));
    expect(api.inviteUser).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Confirm and save" }));
    await waitFor(() => expect(api.inviteUser).toHaveBeenCalledWith({ name: "New Member", email: "new@example.com", roleCode: "tenant_user", tenantId: "tn_a" }));
    expect(await screen.findByRole("dialog", { name: "Temporary password" })).toBeInTheDocument();
  });

  it("confirms a real deactivate request instead of displaying a placeholder message", async () => {
    const user = userEvent.setup(); render(<Security />);
    await user.click(await screen.findByRole("button", { name: "Actions for Team Member" }));
    await user.click(screen.getByRole("menuitem", { name: "Deactivate" }));
    const dialog = screen.getByRole("dialog", { name: "Deactivate account" });
    expect(api.updateUser).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(api.updateUser).toHaveBeenCalledWith("member", { status: "deactivated" }));
  });
});
