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
