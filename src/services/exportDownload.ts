import { downloadFile } from "@/services/fileDownload";

export type StructuredExportFormat = "csv" | "xlsx";
export type OperationalExportType = "subscriptions" | "invoices" | "conversations";

export interface OperationalExportFilters {
  search?: string;
  status?: string;
  plan?: string;
  tenantId?: string;
  botId?: string;
  sentiment?: string;
  contained?: boolean;
  flagged?: boolean;
}

const MIME_BY_FORMAT: Record<StructuredExportFormat, string> = {
  csv: "text/csv",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
};

function appendFilters(
  params: URLSearchParams,
  filters: OperationalExportFilters,
) {
  for (const [key, value] of Object.entries(filters)) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
}

export async function downloadOperationalExport(
  exportType: OperationalExportType,
  format: StructuredExportFormat,
  filters: OperationalExportFilters = {},
): Promise<string> {
  const params = new URLSearchParams({ format });
  appendFilters(params, filters);
  return downloadFile({
    url: `/api/v1/exports/${encodeURIComponent(exportType)}?${params.toString()}`,
    fallbackFilename: `echosphere-${exportType}.${format}`,
    accept: MIME_BY_FORMAT[format],
    expectedContentTypes: [MIME_BY_FORMAT[format]],
  });
}

export async function downloadConversationTranscript(
  conversationId: string,
  format: StructuredExportFormat,
): Promise<string> {
  const params = new URLSearchParams({ format });
  return downloadFile({
    url: `/api/v1/conversations/${encodeURIComponent(conversationId)}/transcript/export?${params}`,
    fallbackFilename: `echosphere-transcript-${conversationId}.${format}`,
    accept: MIME_BY_FORMAT[format],
    expectedContentTypes: [MIME_BY_FORMAT[format]],
  });
}

export async function downloadInvoicePdf(invoiceId: string): Promise<string> {
  return downloadFile({
    url: `/api/v1/invoices/${encodeURIComponent(invoiceId)}/pdf`,
    fallbackFilename: `echosphere-invoice-${invoiceId}.pdf`,
    accept: "application/pdf",
    expectedContentTypes: ["application/pdf"],
  });
}
