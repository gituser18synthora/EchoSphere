/* Feature flags for capabilities whose backend contract is not
   confirmed yet. Each flag maps to an entry in TODO_BACKEND.md.
   Flip to true once the endpoint lands; UI already handles both. */

export const flags = {
  /** Real-time call monitoring stream (WebSocket) */
  liveCallFeed: false,
  /** Server-side voice sample synthesis for previews */
  voiceSamplePlayback: false,
  /** Scheduled publish (server-side cron) */
  scheduledPublish: false,
  /** Recording audio playback (signed URLs) */
  recordingPlayback: false,
  /** Knowledge connector OAuth flows (Zendesk, Confluence…) */
  knowledgeConnectors: false,
  /** Per-conversation cost visibility for tenant admins */
  tenantCostVisibility: true,
  /** CSV/PDF export generation */
  exportGeneration: false,
} as const;

export type FlagName = keyof typeof flags;
