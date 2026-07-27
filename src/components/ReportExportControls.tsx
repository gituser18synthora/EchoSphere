import { ExportControls } from "@/components/ExportControls";
import {
  downloadReport,
  type ReportExportFilters,
  type ReportExportFormat,
  type ReportType,
} from "@/services/reportDownload";

export function ReportExportControls({
  reportType,
  filters,
}: {
  reportType: ReportType;
  filters: ReportExportFilters;
}) {
  return (
    <ExportControls
      onDownload={(format: ReportExportFormat) =>
        downloadReport(reportType, format, filters)}
    />
  );
}
