import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  downloadConversationTranscript,
  downloadInvoicePdf,
  downloadOperationalExport,
} from "@/services/exportDownload";

describe("operational download service", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    vi.stubGlobal("fetch", vi.fn());
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:export"),
      revokeObjectURL: vi.fn(),
    });
  });

  it("sends the selected format and complete active filters", async () => {
    localStorage.setItem("echosphere.token", "tenant-token");
    vi.mocked(fetch).mockResolvedValue(new Response(new Blob(["Call ID\r\n"]), {
      status: 200,
      headers: {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": 'attachment; filename="tenant-conversations.csv"',
      },
    }));
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    const filename = await downloadOperationalExport("conversations", "csv", {
      search: "billing",
      botId: "bot-102",
      contained: false,
      flagged: true,
    });

    expect(filename).toBe("tenant-conversations.csv");
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/exports/conversations?format=csv&search=billing&botId=bot-102&contained=false&flagged=true",
      expect.objectContaining({
        method: "GET",
        headers: expect.objectContaining({
          Accept: "text/csv",
          Authorization: "Bearer tenant-token",
        }),
      }),
    );
  });

  it("downloads transcript Excel and invoice PDF with their real MIME types", async () => {
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(new Blob(["xlsx"]), {
        status: 200,
        headers: {
          "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
      }))
      .mockResolvedValueOnce(new Response(new Blob(["%PDF-1.7"]), {
        status: 200,
        headers: {
          "Content-Type": "application/pdf",
          "Content-Disposition": 'attachment; filename="invoice-001.pdf"',
        },
      }));

    await expect(downloadConversationTranscript("cv-001", "xlsx"))
      .resolves.toBe("echosphere-transcript-cv-001.xlsx");
    await expect(downloadInvoicePdf("inv-001"))
      .resolves.toBe("invoice-001.pdf");

    expect(fetch).toHaveBeenNthCalledWith(
      1,
      "/api/v1/conversations/cv-001/transcript/export?format=xlsx",
      expect.any(Object),
    );
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/api/v1/invoices/inv-001/pdf",
      expect.any(Object),
    );
  });

  it("does not download validation JSON as a file", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({
      success: false,
      message: "Validation failed.",
      errors: [{ field: "status", message: "Unsupported status." }],
    }), {
      status: 422,
      headers: { "Content-Type": "application/json" },
    }));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click");

    await expect(downloadOperationalExport("subscriptions", "xlsx", {
      status: "bad-status",
    })).rejects.toThrow("Validation failed. (status: Unsupported status.)");
    expect(click).not.toHaveBeenCalled();
  });
});
