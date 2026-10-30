/* Sidebar navigation registry: Regional & Currency Settings is a Super
   Admin item placed in the Platform group next to Platform Configuration,
   and never appears for tenant roles. (The route itself is enforced by the
   /admin Guard and the backend master-data permissions — the menu is not
   the security boundary.) */

import { describe, expect, it } from "vitest";
import { navFor } from "./AppShell";

describe("Super Admin navigation", () => {
  it("offers Regional & Currency Settings next to Platform Configuration", () => {
    const sections = navFor("super_admin", 0);
    const platform = sections.find((s) => s.title === "Platform")!;
    const labels = platform.items.map((i) => i.label);
    const config = labels.indexOf("Platform Configuration");
    const regional = labels.indexOf("Regional & Currency Settings");
    expect(config).toBeGreaterThanOrEqual(0);
    expect(regional).toBe(config + 1);
    expect(platform.items[regional].to).toBe("/admin/regional-settings");
  });

  it("never shows the Super Admin configuration items to tenant roles", () => {
    for (const role of ["tenant_admin", "tenant_user"] as const) {
      const labels = navFor(role, 0).flatMap((s) => s.items.map((i) => i.label));
      expect(labels).not.toContain("Regional & Currency Settings");
      expect(labels).not.toContain("Platform Configuration");
    }
  });
});

/* Permission-gated tenant navigation: entries carrying a perm code appear
   only when the session holds it. The default hasPermission (used above)
   grants everything, matching the pre-permission behavior for admins. */
describe("Tenant navigation permission gating", () => {
  const TENANT_ADMIN_PERMS = [
    "manage_voice_clones", "manage_channels", "integrations.manage", "settings.manage",
    "team.manage",
  ];
  const TENANT_USER_PERMS = [
    "bots.view", "knowledge.view", "conversations.view", "analytics.view",
    "manage_knowledge", "manage_prompts", "manage_voices",
    "manage_workflows", "manage_testing",
  ];

  it("keeps every entry for a tenant admin holding the management permissions", () => {
    const labels = navFor("tenant_admin", 0, (c) => TENANT_ADMIN_PERMS.includes(c))
      .flatMap((s) => s.items.map((i) => i.label));
    for (const label of ["Cloned Voices", "Channels", "Integrations", "Settings", "Team"]) {
      expect(labels).toContain(label);
    }
  });

  it("hides Cloned Voices, Channels, Team, Integrations and Settings from a tenant user", () => {
    const sections = navFor("tenant_user", 0, (c) => TENANT_USER_PERMS.includes(c));
    const labels = sections.flatMap((s) => s.items.map((i) => i.label));
    for (const label of ["Cloned Voices", "Channels", "Team", "Integrations", "Settings"]) {
      expect(labels).not.toContain(label);
    }
    // Allowed areas stay reachable.
    for (const label of ["Dashboard", "My VoiceBots", "Knowledge Hub", "Workflows",
                         "Analytics", "Conversation Review"]) {
      expect(labels).toContain(label);
    }
  });
});
