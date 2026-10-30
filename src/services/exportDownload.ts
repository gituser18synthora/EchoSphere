import { downloadFile } from "@/services/fileDownload";

export type StructuredExportFormat = "csv" | "xlsx";
/** csv/xlsx are per-turn analytics tables; pdf/txt are the readable
    conversation document meant for forwarding to someone. */
export type TranscriptExportFormat = StructuredExportFormat | "pdf" | "txt";
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
  /** Inclusive `startedAt` bounds as ISO-8601 instants (conversations only). */
  dateFrom?: string;
  dateTo?: string;
}

const MIME_BY_FORMAT: Record<StructuredExportFormat, string> = {
  csv: "text/csv",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
};

const TRANSCRIPT_MIME_BY_FORMAT: Record<TranscriptExportFormat, string> = {
  ...MIME_BY_FORMAT,
  pdf: "application/pdf",
  txt: "text/plain",
};

/** The viewer's IANA timezone, so a downloaded document prints the same clock
    times as the page it came from (the server has no other way to know). */
function viewerTimeZone(): string | undefined {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
  } catch {
    return undefined;
  }
}

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
  format: TranscriptExportFormat,
): Promise<string> {
  const params = new URLSearchParams({ format });
  const isDocument = format === "pdf" || format === "txt";
  // Only the readable documents print clock times.
  const timeZone = isDocument ? viewerTimeZone() : undefined;
  if (timeZone) params.set("tz", timeZone);
  const mime = TRANSCRIPT_MIME_BY_FORMAT[format];
  return downloadFile({
    url: `/api/v1/conversations/${encodeURIComponent(conversationId)}/transcript/export?${params}`,
    fallbackFilename: `echosphere-${isDocument ? "conversation" : "transcript"}-${conversationId}.${format}`,
    accept: mime,
    expectedContentTypes: [mime],
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
