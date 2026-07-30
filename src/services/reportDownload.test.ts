import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  downloadReport,
  filenameFromDisposition,
} from "@/services/reportDownload";

describe("report download service", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    vi.stubGlobal("fetch", vi.fn());
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:report"),
      revokeObjectURL: vi.fn(),
    });
  });

  it("parses and sanitizes server filenames", () => {
    expect(filenameFromDisposition(
      "attachment; filename=\"echosphere-usage.csv\"",
      "fallback.csv",
    )).toBe("echosphere-usage.csv");
    expect(filenameFromDisposition(
      "attachment; filename*=UTF-8''AI%20Cost.xlsx",
      "fallback.xlsx",
    )).toBe("AI Cost.xlsx");
    expect(filenameFromDisposition(
      "attachment; filename=\"../../unsafe.csv\"",
      "fallback.csv",
    )).toBe("unsafe.csv");
  });

  it("downloads a binary response using the server filename", async () => {
    localStorage.setItem("echosphere.token", "token-value");
    vi.mocked(fetch).mockResolvedValue(new Response(new Blob(["a,b\r\n1,2"]), {
      status: 200,
      headers: {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": 'attachment; filename="server-usage.csv"',
      },
    }));
    let clickedFilename = "";
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clickedFilename = this.download;
    });

    const filename = await downloadReport("usage", "csv", { days: 30 });

    expect(filename).toBe("server-usage.csv");
    expect(clickedFilename).toBe("server-usage.csv");
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/reports/usage/export?format=csv&days=30",
      expect.objectContaining({
        method: "GET",
        headers: expect.objectContaining({ Authorization: "Bearer token-value" }),
      }),
    );
  });

  it("does not download an API error JSON as a spreadsheet", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({
      success: false,
      message: "Validation failed.",
      errors: [{ field: "days", message: "Must be at least 7." }],
    }), {
      status: 422,
      headers: { "Content-Type": "application/json" },
    }));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click");

    await expect(downloadReport("ai_cost", "xlsx", { days: 2 }))
      .rejects.toThrow("Validation failed. (days: Must be at least 7.)");
    expect(click).not.toHaveBeenCalled();
  });

  it("rejects a successful JSON response instead of creating a fake file", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response("{}", {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    await expect(downloadReport("revenue", "xlsx", { days: 30 }))
      .rejects.toThrow("unexpected file type");
  });
});
