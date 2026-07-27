import { downloadFile } from "@/services/fileDownload";

export { filenameFromDisposition } from "@/services/fileDownload";

export type ReportType = "usage" | "revenue" | "ai_cost";
export type ReportExportFormat = "csv" | "xlsx";

export interface ReportExportFilters {
  days: number;
  tenantId?: string;
  botId?: string;
}

function fallbackFilename(reportType: ReportType, format: ReportExportFormat): string {
  return `echosphere-${reportType.replace(/_/g, "-")}.${format}`;
}

export async function downloadReport(
  reportType: ReportType,
  format: ReportExportFormat,
  filters: ReportExportFilters,
): Promise<string> {
  const params = new URLSearchParams({
    format,
    days: String(filters.days),
  });
  if (filters.tenantId) params.set("tenantId", filters.tenantId);
  if (filters.botId) params.set("botId", filters.botId);

  const expected = format === "csv"
    ? "text/csv"
    : "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
  return downloadFile({
    url: `/api/v1/reports/${encodeURIComponent(reportType)}/export?${params.toString()}`,
    fallbackFilename: fallbackFilename(reportType, format),
    accept: expected,
    expectedContentTypes: [expected],
  });
}
