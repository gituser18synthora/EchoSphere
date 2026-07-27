import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ReportExportControls } from "@/components/ReportExportControls";
import * as reportApi from "@/services/reportDownload";

const toast = vi.fn();

vi.mock("@/services/reportDownload", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/reportDownload")>();
  return { ...actual, downloadReport: vi.fn() };
});
vi.mock("@/state/AppContext", () => ({
  useApp: () => ({ toast }),
}));

describe("ReportExportControls", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(reportApi.downloadReport).mockResolvedValue("usage.csv");
  });

  it("offers CSV and Excel and sends the selected report and filters", async () => {
    const user = userEvent.setup();
    render(<ReportExportControls reportType="ai_cost" filters={{ days: 90, botId: "bot-1" }} />);

    const format = screen.getByLabelText("Export format");
    expect(format).toHaveValue("csv");
    expect(screen.getByRole("option", { name: "CSV" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Excel" })).toBeInTheDocument();

    await user.selectOptions(format, "xlsx");
    await user.click(screen.getByRole("button", { name: "Download" }));
    expect(reportApi.downloadReport).toHaveBeenCalledWith(
      "ai_cost",
      "xlsx",
      { days: 90, botId: "bot-1" },
    );
  });

  it("shows a loading state and prevents duplicate downloads", async () => {
    let resolve!: (value: string) => void;
    vi.mocked(reportApi.downloadReport).mockReturnValue(
      new Promise<string>((done) => { resolve = done; }),
    );
    const user = userEvent.setup();
    render(<ReportExportControls reportType="usage" filters={{ days: 30 }} />);
    const button = screen.getByRole("button", { name: "Download" });

    await user.click(button);
    await waitFor(() => expect(button).toBeDisabled());
    await user.click(button);
    expect(reportApi.downloadReport).toHaveBeenCalledTimes(1);

    resolve("usage.csv");
    await waitFor(() => expect(button).not.toBeDisabled());
  });

  it("surfaces permission and validation errors instead of downloading silently", async () => {
    vi.mocked(reportApi.downloadReport).mockRejectedValue(
      new Error("You do not have permission to perform this action."),
    );
    const user = userEvent.setup();
    render(<ReportExportControls reportType="usage" filters={{ days: 30 }} />);
    await user.click(screen.getByRole("button", { name: "Download" }));

    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      "You do not have permission to perform this action.",
      "error",
    ));
  });
});
