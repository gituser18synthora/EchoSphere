# TODO-BACKEND — endpoints the new UI needs but that are not confirmed to exist

The frontend never invents endpoints. Every capability below is implemented
against a **typed service interface** (`src/services/api.ts`) with a mock body
and, where the UX depends on it, a **feature flag** (`src/services/flags.ts`).
Replacing a mock with a real call must not change any component.

| # | Capability | Service function | Flag | Notes |
|---|------------|------------------|------|-------|
| 1 | Live call feed on dashboards | `—` (WebSocket) | `liveCallFeed` | UI shows "live" badge only when flag is on. |
| 2 | Voice sample synthesis | `listVoices` + playback URL | `voiceSamplePlayback` | Voice cards render a play button; disabled with tooltip until flag on. |
| 3 | Scheduled publish | `Release.scheduledFor` | `scheduledPublish` | Publish Center shows the schedule picker behind the flag. |
| 4 | ~~Call recording playback~~ **shipped 2026-07-30** | `getConversation().recording` + `GET /conversations/{id}/recording` | — | Voice worker records calls (stereo WAV, caller L / bot R) under `storage/recordings/`; drawer shows a native player + authorized download, graceful when absent. |
| 5 | Knowledge connectors (Zendesk, Confluence, SharePoint OAuth) | `listKnowledge` type `connector` | `knowledgeConnectors` | "Connect source" CTA disabled with explanation until flag on. |
| 6 | Optional background exports for unusually large datasets | `—` | `exportGeneration` | Every visible export is functional through authorized synchronous CSV/XLSX endpoints. A queued worker may be added later for datasets that outgrow synchronous delivery; no current button depends on it. |
| 7 | Tenant provisioning orchestration | onboarding wizard step 7 | — | Wizard simulates retryable provisioning tasks; real API must expose per-task status + retry. |
| 8 | Prompt approval workflow | `Prompt.state` transitions | — | UI models draft → pending_approval → approved; backend must enforce RBAC on the transition. |
| 9 | Release approval + rollback | `listReleases` mutations | — | Stage machine draft → review → approved → published → rolled_back; server must gate `published` on checklist. |
| 10 | Regression suite execution | `listScenarios` + run trigger | — | "Run suite" simulates; backend needs an async job + per-step trace. |
| 11 | API connection test console | `testApiConnection` | — | Mock differentiates healthy/failing; real endpoint must never echo raw secrets. |
| 12 | Global search across tenants/bots/conversations | header search | — | Currently client-side over loaded fixtures. |

## Contract-preservation notes

- All identifiers (`tn-*`, `bot-*`, …) are opaque strings; no format is assumed.
- Secrets appear only as `secret://` references; the UI masks and never stores raw values.
- Tenant-admin surfaces exclude model/provider config, embeddings, token limits,
  system prompts, guardrail internals, and global credentials **at the service
  layer** (those functions simply aren't imported by tenant routes), not just via CSS.
