/* Import Tenant JSON — the "paste" side of tenant Copy/Paste deployment.

   Select the tenant_<id>.json file downloaded by Export Tenant JSON on the
   source environment, review what the package contains, and import it. The
   backend upserts everything PRESERVING ids: a missing tenant is created with
   the exported ids, an existing one is updated in place (idempotent — no
   duplicates), and id collisions with other tenants abort the import. */

import { useRef, useState } from "react";
import {
  importTenantPackage,
  type TenantExportPackage,
  type TenantImportReport,
} from "@/services/api";
import {
  parseTenantPackage,
  type TenantPackageSummary,
} from "@/services/tenantTransfer";
import { Button, Callout, Modal } from "@/components/ui";
import { Icon } from "@/components/Icon";

export function ImportTenantModal({ open, onClose, onImported }: {
  open: boolean;
  onClose: () => void;
  /** Called once per successful import so the tenant list can refresh. */
  onImported: (report: TenantImportReport) => void;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [pkg, setPkg] = useState<TenantExportPackage | null>(null);
  const [summary, setSummary] = useState<TenantPackageSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState<TenantImportReport | null>(null);

  const reset = () => {
    setFileName(null);
    setPkg(null);
    setSummary(null);
    setError(null);
    setReport(null);
  };

  const close = () => {
    if (busy) return;
    reset();
    onClose();
  };

  const onFile: React.ChangeEventHandler<HTMLInputElement> = async (ev) => {
    const file = ev.target.files?.[0];
    ev.target.value = "";
    if (!file) return;
    setFileName(file.name);
    setError(null);
    setReport(null);
    try {
      const parsed = parseTenantPackage(await file.text());
      setPkg(parsed.pkg);
      setSummary(parsed.summary);
    } catch (e) {
      setPkg(null);
      setSummary(null);
      setError(e instanceof Error ? e.message : "Could not read that file.");
    }
  };

  const importNow = async () => {
    if (!pkg) return;
    setBusy(true);
    setError(null);
    try {
      const result = await importTenantPackage(pkg);
      setReport(result);
      onImported(result);
    } catch (e) {
      // Surface the backend's exact message (collision, schema version,
      // secret-reference and validation errors) — never a generic failure.
      setError(e instanceof Error ? e.message : "Import failed.");
    } finally {
      setBusy(false);
    }
  };

  const botsImported = report
    ? (report.created.bot ?? 0) + (report.updated.bot ?? 0)
    : 0;

  return (
    <Modal
      open={open}
      onClose={close}
      title="Import Tenant JSON"
      sub="Deploy a tenant exported from another environment — all ids are preserved. Re-importing the same file updates the tenant in place, never duplicates it."
      footer={report ? (
        <Button variant="primary" onClick={close}>Done</Button>
      ) : (
        <>
          <Button variant="ghost" onClick={close} disabled={busy}>Cancel</Button>
          <Button variant="primary" icon="upload" busy={busy} disabled={!pkg} onClick={importNow}>
            Import Tenant
          </Button>
        </>
      )}
    >
      {report ? (
        <>
          <Callout tone="good" title="Tenant imported successfully">
            <div className="mt-4">Tenant: <code className="t-num">{report.tenantId}</code></div>
            <div>Bots imported: <b>{botsImported}</b></div>
            {report.knowledgeDocuments > 0 && (
              <div>Knowledge documents: <b>{report.knowledgeDocuments}</b></div>
            )}
          </Callout>
          <div className="t-sub mt-12" style={{ fontSize: 13 }}>
            {Object.values(report.created).reduce((a, b) => a + b, 0)} resources created,{" "}
            {Object.values(report.updated).reduce((a, b) => a + b, 0)} updated,{" "}
            {Object.values(report.reused).reduce((a, b) => a + b, 0)} shared platform resources reused.
            The tenant keeps exactly the ids from the package.
          </div>
          {report.warnings.length > 0 && (
            <div className="mt-12">
              <Callout tone="warning" title="Warnings">
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {report.warnings.map((w) => <li key={w}>{w}</li>)}
                </ul>
              </Callout>
            </div>
          )}
        </>
      ) : (
        <>
          <div className="dropzone" role="group" aria-label="Tenant package file">
            <span className="dropzone-icon"><Icon name="upload" size={20} /></span>
            <span className="t-strong" style={{ fontSize: 13 }}>
              {fileName ?? "Select the tenant_<id>.json exported from the source environment"}
            </span>
            <Button icon="file" disabled={busy} onClick={() => fileRef.current?.click()}>
              Choose JSON file
            </Button>
            <input
              ref={fileRef}
              type="file"
              accept=".json,application/json"
              style={{ display: "none" }}
              aria-label="Choose tenant JSON file"
              onChange={onFile}
            />
          </div>

          {error && (
            <div className="mt-12">
              <Callout tone="critical" title="Import failed">{error}</Callout>
            </div>
          )}

          {summary && !error && (
            <div className="mt-12">
              <Callout tone="info" title={`${summary.tenantName} — ready to import`}>
                <div className="mt-4">Tenant id: <code className="t-num">{summary.tenantId}</code> · schema v{summary.schemaVersion}</div>
                <div className="mt-4">
                  {summary.counts.filter((c) => c.value > 0).map((c) => `${c.value} ${c.label.toLowerCase()}`).join(" · ") || "no resources"}
                </div>
                {summary.bots.length > 0 && (
                  <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                    {summary.bots.map((b) => (
                      <li key={b.id}>{b.name} <code className="t-micro">{b.id}</code></li>
                    ))}
                  </ul>
                )}
              </Callout>
            </div>
          )}
        </>
      )}
    </Modal>
  );
}
