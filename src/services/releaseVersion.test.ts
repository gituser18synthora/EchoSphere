import { describe, expect, it } from "vitest";
import { nextVersion, openRelease, suggestedVersion } from "@/services/releaseVersion";
import type { Release } from "@/types/domain";

const rel = (version: string, stage: Release["stage"]): Release =>
  ({ id: `rel_${version}_${stage}`, botId: "bot_x", version, stage, notes: "", requestedBy: "", checklist: [], diff: [] });

describe("releaseVersion", () => {
  it("bumps the patch component and keeps the v prefix style", () => {
    expect(nextVersion("v0.1.0")).toBe("v0.1.1");
    expect(nextVersion("1.2.9")).toBe("1.2.10");
  });

  it("falls back for non-semver or empty input", () => {
    expect(nextVersion("beta")).toBe("beta");
    expect(nextVersion(undefined)).toBe("v0.1.0");
    expect(nextVersion("")).toBe("v0.1.0");
  });

  it("proposes the bot's own version for the first release, then bumps past the latest", () => {
    expect(suggestedVersion({ version: "v0.1.0" }, [])).toBe("v0.1.0");
    expect(suggestedVersion({ version: "v0.1.0" }, [rel("v0.1.0", "published"), rel("v0.1.10", "rolled_back"), rel("v0.1.2", "published")]))
      .toBe("v0.1.11");
  });

  it("finds the single in-flight release regardless of order", () => {
    expect(openRelease([rel("v1", "published"), rel("v2", "approved")])?.version).toBe("v2");
    expect(openRelease([rel("v1", "published"), rel("v0", "rolled_back")])).toBeUndefined();
  });
});
