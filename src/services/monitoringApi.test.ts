import { afterEach, expect, it, vi } from "vitest";
import { listAlertsPage } from "@/services/api";

afterEach(() => vi.unstubAllGlobals());

it("retains server-wide status counts alongside a filtered page", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
    success: true,
    data: [{ id: "resolved" }],
    meta: { page: 2, pageSize: 25, total: 26, totalPages: 2,
      counts: { open: 110, acknowledged: 4, resolved: 26, total: 140 } },
  }), { status: 200, headers: { "Content-Type": "application/json" } })));
  const result = await listAlertsPage({ status: "resolved", page: 2, pageSize: 25 });
  expect(result.meta.counts).toEqual({ open: 110, acknowledged: 4, resolved: 26, total: 140 });
  expect(result.meta.total).toBe(26);
  expect(result.meta.page).toBe(2);
  expect(result.items).toEqual([{ id: "resolved" }]);
});
