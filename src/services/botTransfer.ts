/* Bot Export / Import helpers.

   The downloaded `bot_<id>.json` file is the transfer artifact: export it on
   the source environment (My VoiceBots → bot menu → Export bot JSON), then
   select the same file on the target environment (My VoiceBots → Import bot).
   The backend keeps tenant_id + bot_id: the bot is created with the same id,
   or the existing bot of that id is updated in place — same tenant only. */

import { exportBotPackage, type BotExportPackage } from "@/services/api";

export const BOT_PACKAGE_KIND = "echosphere.bot.export";

export const botPackageFilename = (botId: string) => `bot_${botId}.json`;

export interface BotPackageSummary {
  tenantId: string;
  botId: string;
  botName: string;
  schemaVersion: number;
  exportedAt: string | null;
  sourceTenantName: string | null;
  /** Resource counts shown in the import preview, in display order. */
  counts: { label: string; value: number }[];
}

function listLength(section: unknown): number {
  return Array.isArray(section) ? section.length : 0;
}

export function summarizeBotPackage(pkg: BotExportPackage): BotPackageSummary {
  const resources = pkg.resources ?? {};
  const prompts = Array.isArray(resources.prompts) ? (resources.prompts as { versions?: unknown[] }[]) : [];
  const versions = prompts.reduce((n, p) => n + listLength(p.versions), 0);
  const shared = pkg.shared ?? {};
  return {
    tenantId: pkg.tenant_id,
    botId: pkg.bot_id,
    botName: pkg.bot?.name ?? pkg.source?.bot_name ?? pkg.bot_id,
    schemaVersion: pkg.schema_version,
    exportedAt: pkg.exported_at ?? null,
    sourceTenantName: pkg.source?.tenant_name ?? null,
    counts: [
      { label: "Workflows", value: listLength(resources.workflows) },
      { label: "Prompts", value: prompts.length },
      { label: "Prompt versions", value: versions },
      { label: "Intents", value: listLength(resources.intents) },
      { label: "Bot tools (API)", value: listLength(resources.api_connections) },
      { label: "Bot knowledge sources", value: listLength(resources.knowledge_sources) },
      { label: "Knowledge documents", value: pkg.knowledge_plane?.documents?.length ?? 0 },
      { label: "Test scenarios", value: listLength(resources.test_scenarios) },
      { label: "Releases", value: listLength(resources.releases) },
      { label: "Shared tools referenced", value: listLength(shared.api_connections) },
      { label: "Entities referenced", value: listLength(shared.entity_defs) },
      { label: "Channels (environment)", value: listLength(pkg.environment?.channel_configs) },
    ],
  };
}

/** Parse an uploaded bot package file. Throws a user-readable error for
    anything that is not a complete bot export; the backend re-validates
    everything (tenant, ids, integrity seal) before writing. */
export function parseBotPackage(raw: string): { pkg: BotExportPackage; summary: BotPackageSummary } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error("This file is not valid JSON. Select the bot_<id>.json file downloaded by Export bot JSON.");
  }
  const pkg = parsed as BotExportPackage;
  if (!pkg || typeof pkg !== "object" || Array.isArray(pkg) || pkg.kind !== BOT_PACKAGE_KIND) {
    const hint = (pkg as { kind?: string } | null)?.kind === "echosphere.tenant.export"
      ? " This is a tenant package — import it from Admin → Organizations."
      : "";
    throw new Error(`This JSON is not a bot export package (expected kind "${BOT_PACKAGE_KIND}").${hint}`);
  }
  if (typeof pkg.schema_version !== "number") {
    throw new Error("The package has no schema_version — the file looks incomplete.");
  }
  if (typeof pkg.tenant_id !== "string" || typeof pkg.bot_id !== "string" || !pkg.bot?.id) {
    throw new Error("The package has no tenant_id / bot_id — the file looks incomplete.");
  }
  if (!pkg.integrity) {
    throw new Error("The package has no integrity seal — re-export the bot instead of editing the file.");
  }
  return { pkg, summary: summarizeBotPackage(pkg) };
}

/** Fetch the complete package (knowledge included) and save it as one
    bot_<id>.json file via the browser's download flow. */
export async function downloadBotPackage(botId: string): Promise<{ filename: string; summary: BotPackageSummary }> {
  const pkg = await exportBotPackage(botId);
  const filename = botPackageFilename(pkg.bot_id ?? botId);
  const blob = new Blob([JSON.stringify(pkg, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  // Revoking synchronously can cancel a download in some browsers.
  window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
  return { filename, summary: summarizeBotPackage(pkg) };
}

/** Human labels for the backend's resource keys in import reports. */
const RESOURCE_LABELS: Record<string, string> = {
  bot: "bot",
  workflow: "workflows",
  prompt: "prompts",
  prompt_version: "prompt versions",
  intent: "intents",
  api_connection: "bot tools",
  tenant_api_connection: "shared tools",
  knowledge_source: "knowledge sources",
  test_scenario: "test scenarios",
  release: "releases",
  voice_bot_settings: "voice settings",
  runtime_context_schema: "runtime context schema",
  channel_config: "channels",
  phone_number: "phone numbers",
  guardrail: "guardrails",
  guardrail_profile: "guardrail profiles",
  platform_voice_profile: "platform voices",
  tenant_voice_profile: "cloned voices",
  entity_def: "entities",
};

export function describeCounts(counts: Record<string, number>): string {
  return Object.entries(counts)
    .filter(([, n]) => n > 0)
    .map(([key, n]) => `${n} ${RESOURCE_LABELS[key] ?? key.replace(/_/g, " ")}`)
    .join(" · ");
}
