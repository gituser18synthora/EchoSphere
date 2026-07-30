import { useState } from "react";
import { Button } from "@/components/ui";
import { useApp } from "@/state/AppContext";
import type { StructuredExportFormat } from "@/services/exportDownload";

export function ExportControls({
  onDownload,
  buttonLabel = "Download",
  formatLabel = "Export format",
}: {
  onDownload: (format: StructuredExportFormat) => Promise<string>;
  buttonLabel?: string;
  formatLabel?: string;
}) {
  const [format, setFormat] = useState<StructuredExportFormat>("csv");
  const [busy, setBusy] = useState(false);
  const { toast } = useApp();

  const startDownload = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const filename = await onDownload(format);
      toast(`Downloaded ${filename}`);
    } catch (error) {
      toast(error instanceof Error ? error.message : "Download failed.", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="row gap-8">
      <label className="row gap-6">
        <span className="t-micro">Format</span>
        <select
          className="select"
          aria-label={formatLabel}
          value={format}
          disabled={busy}
          onChange={(event) =>
            setFormat(event.target.value as StructuredExportFormat)
          }
          style={{ minWidth: 94 }}
        >
          <option value="csv">CSV</option>
          <option value="xlsx">Excel</option>
        </select>
      </label>
      <Button
        icon="download"
        busy={busy}
        onClick={() => void startDownload()}
      >
        {buttonLabel}
      </Button>
    </div>
  );
}
