/* Tenant Copy/Paste deployment helpers.

   The downloaded `tenant_<id>.json` file is the transfer artifact: export it
   on the source environment (Tenant detail → Export Tenant JSON), then select
   the same file on the target environment (Organizations → Import Tenant
   JSON). The backend preserves every id in the package, so the tenant and its
   bots keep exactly the same identifiers after deployment. */

import {
  exportTenantPackage,
  type TenantExportPackage,
} from "@/services/api";

export const TENANT_PACKAGE_KIND = "echosphere.tenant.export";

export const tenantPackageFilename = (tenantId: string) => `tenant_${tenantId}.json`;

export interface TenantPackageSummary {
  tenantId: string;
  tenantName: string;
  schemaVersion: number;
  bots: { id: string; name: string }[];
  /** Resource counts shown in the import preview, in display order. */
  counts: { label: string; value: number }[];
}

function sectionLength(resources: Record<string, unknown>, key: string): number {
  const value = resources[key];
  return Array.isArray(value) ? value.length : 0;
}

export function summarizeTenantPackage(pkg: TenantExportPackage): TenantPackageSummary {
  const resources = pkg.resources as Record<string, unknown> & TenantExportPackage["resources"];
  const bots = (resources.bots ?? []).map((b) => ({ id: b.id, name: b.name ?? b.id }));
  return {
    tenantId: resources.tenant.id,
    tenantName: resources.tenant.name ?? resources.tenant.id,
    schemaVersion: pkg.schema_version,
    bots,
    counts: [
      { label: "Bots", value: bots.length },
      { label: "Workflows", value: sectionLength(resources, "workflows") },
      { label: "Prompts", value: sectionLength(resources, "prompts") },
      { label: "Intents", value: sectionLength(resources, "intents") },
      { label: "API connections", value: sectionLength(resources, "api_connections") },
      { label: "Channels", value: sectionLength(resources, "channel_configs") },
      { label: "Knowledge sources", value: sectionLength(resources, "knowledge_sources") },
      { label: "Knowledge documents", value: pkg.knowledge_plane?.documents?.length ?? 0 },
    ],
  };
}

/** Parse an uploaded tenant package file. Throws a user-readable error for
    anything that is not a complete tenant export. */
export function parseTenantPackage(raw: string): {
  pkg: TenantExportPackage;
  summary: TenantPackageSummary;
} {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error(
      "This file is not valid JSON. Select the tenant_<id>.json file downloaded by Export Tenant JSON.",
    );
  }
  const pkg = parsed as TenantExportPackage;
  if (!pkg || typeof pkg !== "object" || Array.isArray(pkg) || pkg.kind !== TENANT_PACKAGE_KIND) {
    throw new Error(
      `This JSON is not a tenant export package (expected kind "${TENANT_PACKAGE_KIND}").`,
    );
  }
  if (typeof pkg.schema_version !== "number") {
    throw new Error("The package has no schema_version — the file looks incomplete.");
  }
  if (!pkg.resources?.tenant?.id) {
    throw new Error("The package has no resources.tenant.id — the file looks incomplete.");
  }
  return { pkg, summary: summarizeTenantPackage(pkg) };
}

/** Fetch the complete package (knowledge included) and save it as one
    tenant_<id>.json file via the browser's download flow. */
export async function downloadTenantPackage(tenantId: string): Promise<{
  filename: string;
  summary: TenantPackageSummary;
}> {
  const pkg = await exportTenantPackage(tenantId);
  const filename = tenantPackageFilename(pkg.resources?.tenant?.id ?? tenantId);
  const blob = new Blob([JSON.stringify(pkg, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  // Revoking synchronously can cancel a download in some browsers.
  window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
  return { filename, summary: summarizeTenantPackage(pkg) };
}
