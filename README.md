# AUREXION EchoSphere — Enterprise VoiceBot Platform (Frontend)

Modern, role-based frontend for the AUREXION EchoSphere multi-tenant VoiceBot
platform. Built from the redesign brief in
`AUREXION_EchoSphere_Advanced_UI_UX_Redesign_Brief.md`.

## Run

```bash
npm install
npm run dev        # http://localhost:5199
npm run build      # typecheck + production bundle
```

No backend required — a typed mock service layer (`src/services/api.ts`)
simulates latency, failures and mutations. Sign in from `/login` as either
persona; the session persists in localStorage.

## Roles & information architecture

**Super Admin** (`/admin`): Dashboard · Tenants (Organizations, Onboarding
wizard, Subscriptions, Billing, Usage) · AI Governance (models, prompt library,
versions, templates, guardrails) · Voice Platform (bots, numbers, SIP,
channels) · Knowledge (global, docs, URLs, embedding monitor) · Workflows
(journeys, intents, entities, actions) · Monitoring · Security (users, roles,
audit) · Reports.

**Tenant Admin** (`/t`): Dashboard · My VoiceBots · **VoiceBot Studio** (a
unified workspace per bot with tabs: Overview, Knowledge, Prompts, Voice,
Intents & Entities, APIs, Workflows, Channels, Testing, Analytics, Publish) ·
Knowledge Hub · Workflows · Channels · Analytics · Conversation Review · Team ·
Integrations · Settings.

RBAC is enforced in routing (`Guard` in `src/App.tsx`) **and** at the service
layer: tenant routes never import model/provider config, embeddings, system
prompts, guardrail internals or global credentials. Secrets appear only as
masked `secret://` references.

## Architecture

```
src/
  types/domain.ts      All API-facing types (the backend contract)
  services/            mockData fixtures · api.ts typed services · flags.ts
  state/AppContext.tsx Session, theme (light/dark), toasts
  hooks/useAsync.ts    Loading / error / reload for every fetch
  components/          Icon set, ui.tsx (buttons, chips, tables, modals,
                       drawers, timeline, wizard, empty/error/skeleton states),
                       DataTable, charts.tsx (SVG line/bar/donut/sparkline
                       with crosshair tooltips, validated palette)
  layouts/AppShell.tsx Sidebar, breadcrumbs, global search (⌘K), alerts, profile
  pages/admin/…        Super Admin screens
  pages/tenant/…       Tenant screens + studio/ tab panels
  styles/              tokens.css (design tokens, both themes) + component CSS
```

Chart colors were validated with the dataviz six-checks (CVD separation,
lightness band, chroma, contrast) in both light and dark mode.

## Backend gaps

Capabilities without a confirmed backend are implemented behind typed service
interfaces, mocks and feature flags — see **`TODO_BACKEND.md`** for the full
list (recording playback, voice sample synthesis, scheduled publish, knowledge
connectors, export jobs, provisioning orchestration, etc.). Flipping a flag in
`src/services/flags.ts` enables the already-built UI once the endpoint lands.
