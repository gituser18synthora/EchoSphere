import type { Release, VoiceBot } from "@/types/domain";

/** Next semantic version after `base` (patch bump); returns `base` unchanged
    when it is not vMAJOR.MINOR.PATCH, and "v0.1.0" when there is no base. */
export function nextVersion(base: string | undefined): string {
  const m = /^(v?)(\d+)\.(\d+)\.(\d+)$/.exec((base ?? "").trim());
  if (!m) return base?.trim() || "v0.1.0";
  return `${m[1]}${m[2]}.${m[3]}.${Number(m[4]) + 1}`;
}

/** The version to propose for a bot's next release: the first release reuses
    the bot's own draft version; later ones bump past the latest release. */
export function suggestedVersion(bot: Pick<VoiceBot, "version">, releases: Pick<Release, "version">[]): string {
  if (releases.length === 0) return bot.version || "v0.1.0";
  const latest = [...releases].sort((a, b) => b.version.localeCompare(a.version, undefined, { numeric: true }))[0];
  return nextVersion(latest?.version ?? bot.version);
}

/** A release still moving through the pipeline (at most one per bot). */
export function openRelease(releases: Release[]): Release | undefined {
  return releases.find((r) => r.stage === "review" || r.stage === "draft" || r.stage === "approved");
}
