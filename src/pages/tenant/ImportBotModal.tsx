/* Import bot — the "paste" side of bot Export / Import.

   Select the bot_<id>.json downloaded on the source environment. The modal
   parses it, asks the backend for a dry-run preview (tenant check, create vs
   update, resources changed, environment values preserved, warnings), and
   only then imports. The bot keeps exactly its tenant_id + bot_id: a missing
   bot is created, an existing one is updated in place — never duplicated. */

import { useEffect, useRef, useState } from "react";
import {
  importBotPackage,
  previewBotImport,
  type BotExportPackage,
  type BotImportReport,
} from "@/services/api";
import { describeCounts, parseBotPackage, type BotPackageSummary } from "@/services/botTransfer";
import { Button, Callout, Modal, Toggle } from "@/components/ui";
import { Icon } from "@/components/Icon";

export function ImportBotModal({ open, onClose, onImported, tenantId }: {
  open: boolean;
  onClose: () => void;
  /** Called once per successful import so the bot list can refresh. */
  onImported: (report: BotImportReport) => void;
  /** Current tenant when known (tenant roles); super admins fall back to the package tenant. */
  tenantId?: string | null;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [pkg, setPkg] = useState<BotExportPackage | null>(null);
  const [summary, setSummary] = useState<BotPackageSummary | null>(null);
  const [preview, setPreview] = useState<BotImportReport | null>(null);
  const [applyEnvironment, setApplyEnvironment] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState<BotImportReport | null>(null);

  const reset = () => {
    setFileName(null);
    setPkg(null);
    setSummary(null);
    setPreview(null);
    setApplyEnvironment(false);
    setError(null);
    setReport(null);
  };

  const close = () => {
    if (busy) return;
    reset();
    onClose();
  };

  const destination = tenantId || pkg?.tenant_id;

  /* The preview is the backend's word on what will happen: it validates the
     tenant and the integrity seal and runs the whole import in a dry run. */
  useEffect(() => {
    if (!pkg || report) return;
    let cancelled = false;
    setPreviewing(true);
    setPreview(null);
    previewBotImport(pkg, { tenantId: destination, applyEnvironment })
      .then((result) => { if (!cancelled) setPreview(result); })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : "Preview failed."); })
      .finally(() => { if (!cancelled) setPreviewing(false); });
    return () => { cancelled = true; };
  }, [pkg, applyEnvironment, destination, report]);

  const onFile: React.ChangeEventHandler<HTMLInputElement> = async (ev) => {
    const file = ev.target.files?.[0];
    ev.target.value = "";
    if (!file) return;
    setFileName(file.name);
    setError(null);
    setReport(null);
    setPreview(null);
    try {
      const parsed = parseBotPackage(await file.text());
      setPkg(parsed.pkg);
      setSummary(parsed.summary);
    } catch (e) {
      setPkg(null);
      setSummary(null);
      setError(e instanceof Error ? e.message : "Could not read that file.");
    }
  };

  const importNow = async () => {
    if (!pkg || !preview) return;
    setBusy(true);
    setError(null);
    try {
      const result = await importBotPackage(pkg, { tenantId: destination, applyEnvironment });
      setReport(result);
      onImported(result);
    } catch (e) {
      // Surface the backend's exact message (tenant mismatch, collision,
      // integrity, schema version, secret-reference errors).
      setError(e instanceof Error ? e.message : "Import failed.");
    } finally {
      setBusy(false);
    }
  };

  const canImport = !!pkg && !!preview && !previewing && !error;
  const actionLabel = preview?.action === "update" ? "Update existing bot" : "Create bot";

  return (
    <Modal
      open={open}
      onClose={close}
      title="Import bot"
      sub="Deploy a bot exported from another environment. The bot keeps its tenant and bot id — an existing bot with the same id is updated in place, never duplicated."
      footer={report ? (
        <Button variant="primary" onClick={close}>Done</Button>
      ) : (
        <>
          <Button variant="ghost" onClick={close} disabled={busy}>Cancel</Button>
          <Button variant={preview?.action === "update" ? "danger" : "primary"} icon="upload" busy={busy} disabled={!canImport} onClick={importNow}>
            {actionLabel}
          </Button>
        </>
      )}
    >
      {report ? (
        <>
          <Callout tone="good" title={report.action === "update" ? "Bot updated" : "Bot created"}>
            <div className="mt-4">{report.botName ?? report.botId} · <code className="t-num">{report.botId}</code></div>
            <div>Tenant: <code className="t-num">{report.tenantId}</code></div>
            {report.knowledgeDocuments > 0 && <div>Knowledge documents: <b>{report.knowledgeDocuments}</b></div>}
          </Callout>
          <ReportBody report={report} />
        </>
      ) : (
        <>
          <div className="dropzone" role="group" aria-label="Bot package file">
            <span className="dropzone-icon"><Icon name="upload" size={20} /></span>
            <span className="t-strong" style={{ fontSize: 13 }}>
              {fileName ?? "Select the bot_<id>.json exported from the source environment"}
            </span>
            <Button icon="file" disabled={busy} onClick={() => fileRef.current?.click()}>
              Choose JSON file
            </Button>
            <input
              ref={fileRef}
              type="file"
              accept=".json,application/json"
              style={{ display: "none" }}
              aria-label="Choose bot JSON file"
              onChange={onFile}
            />
          </div>

          {error && (
            <div className="mt-12">
              <Callout tone="critical" title="Import blocked">{error}</Callout>
            </div>
          )}

          {summary && !error && (
            <div className="mt-12 col gap-12">
              <Callout tone={preview?.action === "update" ? "warning" : "info"} title={previewing ? "Checking this environment…" : preview ? (preview.action === "update" ? `Existing bot will be updated: ${summary.botName}` : `New bot will be created: ${summary.botName}`) : summary.botName}>
                <dl className="import-summary" style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "4px 12px", margin: "6px 0 0" }}>
                  <dt className="t-sub">Tenant ID</dt><dd style={{ margin: 0 }}><code className="t-num">{summary.tenantId}</code>{summary.sourceTenantName ? ` · ${summary.sourceTenantName}` : ""}</dd>
                  <dt className="t-sub">Bot ID</dt><dd style={{ margin: 0 }}><code className="t-num">{summary.botId}</code></dd>
                  <dt className="t-sub">Bot name</dt><dd style={{ margin: 0 }}>{summary.botName}</dd>
                  <dt className="t-sub">Existing bot</dt><dd style={{ margin: 0 }}>{preview ? (preview.existing ? "Yes" : "No") : "…"}</dd>
                  <dt className="t-sub">Action</dt><dd style={{ margin: 0 }}><b>{preview ? (preview.action === "update" ? "Update" : "Create") : "…"}</b></dd>
                  <dt className="t-sub">Package</dt><dd style={{ margin: 0 }}>schema v{summary.schemaVersion}{summary.exportedAt ? ` · exported ${new Date(summary.exportedAt).toLocaleString()}` : ""}</dd>
                </dl>
                <div className="mt-8 t-sub" style={{ fontSize: 12.5 }}>
                  {summary.counts.filter((c) => c.value > 0).map((c) => `${c.value} ${c.label.toLowerCase()}`).join(" · ") || "no resources"}
                </div>
                {preview?.action === "update" && (
                  <p className="mt-8" style={{ margin: "8px 0 0" }}>
                    The package becomes the source of truth for this bot&apos;s configuration: workflows, prompts, intents,
                    tools, knowledge, voice settings, runtime schema, test scenarios and releases are replaced, and bot-owned
                    records that are not in the package are removed. Live channels and phone numbers are kept unless you
                    apply environment values below.
                  </p>
                )}
              </Callout>

              {preview && (
                <div className="col gap-8">
                  <ChangeList title="Will be created" counts={preview.created} />
                  <ChangeList title="Will be updated" counts={preview.updated} />
                  <ChangeList title="Will be removed (no longer in the package)" counts={preview.removed} />
                  <ChangeList title="Shared resources reused" counts={preview.reused} />
                  {preview.preserved.length > 0 && (
                    <Callout tone="info" title="Environment values kept from this environment">
                      <ul style={{ margin: 0, paddingLeft: 18 }}>
                        {preview.preserved.map((p, i) => (
                          <li key={`${p.kind}-${p.label}-${i}`}>
                            <b>{p.kind.replace(/_/g, " ")}</b> {p.label}: {p.reason}
                            {p.differs?.length ? ` (package differs in ${p.differs.join(", ")})` : ""}
                          </li>
                        ))}
                      </ul>
                    </Callout>
                  )}
                  {preview.secretsMissing.length > 0 && (
                    <Callout tone="warning" title="Secrets to configure in this environment">
                      <ul style={{ margin: 0, paddingLeft: 18 }}>
                        {preview.secretsMissing.map((s) => (
                          <li key={`${s.owner}-${s.reference}`}>{s.owner}: <code>{s.reference}</code> does not resolve here</li>
                        ))}
                      </ul>
                    </Callout>
                  )}
                  {preview.warnings.length > 0 && (
                    <Callout tone="warning" title="Warnings">
                      <ul style={{ margin: 0, paddingLeft: 18 }}>
                        {preview.warnings.map((w) => <li key={w}>{w}</li>)}
                      </ul>
                    </Callout>
                  )}
                </div>
              )}

              <Toggle
                checked={applyEnvironment}
                onChange={setApplyEnvironment}
                label="Also apply environment values from the package (channel configuration, API URLs pointing at local hosts). A phone number held by another bot is never taken."
              />
            </div>
          )}
        </>
      )}
    </Modal>
  );
}

function ChangeList({ title, counts }: { title: string; counts: Record<string, number> }) {
  const text = describeCounts(counts);
  if (!text) return null;
  return (
    <div className="t-sub" style={{ fontSize: 13 }}>
      <span className="t-strong">{title}:</span> {text}
    </div>
  );
}

function ReportBody({ report }: { report: BotImportReport }) {
  return (
    <div className="mt-12 col gap-8">
      <ChangeList title="Created" counts={report.created} />
      <ChangeList title="Updated" counts={report.updated} />
      <ChangeList title="Removed" counts={report.removed} />
      <ChangeList title="Shared resources reused" counts={report.reused} />
      {report.preserved.length > 0 && (
        <Callout tone="info" title="Environment values kept">
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {report.preserved.map((p, i) => (
              <li key={`${p.kind}-${p.label}-${i}`}><b>{p.kind.replace(/_/g, " ")}</b> {p.label}: {p.reason}</li>
            ))}
          </ul>
        </Callout>
      )}
      {report.secretsMissing.length > 0 && (
        <Callout tone="warning" title="Configure these secrets in this environment">
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {report.secretsMissing.map((s) => (
              <li key={`${s.owner}-${s.reference}`}>{s.owner}: <code>{s.reference}</code></li>
            ))}
          </ul>
        </Callout>
      )}
      {report.warnings.length > 0 && (
        <Callout tone="warning" title="Warnings">
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {report.warnings.map((w) => <li key={w}>{w}</li>)}
          </ul>
        </Callout>
      )}
    </div>
  );
}
