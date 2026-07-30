# AUREXION EchoSphere — Advanced UI/UX Redesign Brief

## Purpose
Use this brief to redesign the current Aurexion VoiceBot product into **AUREXION EchoSphere**, an enterprise-ready, role-based VoiceBot workspace. This brief is based on the supplied product-flow document and the product demonstration video.

The redesign must retain useful existing functional modules seen in the demo—VoiceBot inventory, bot overview/configuration, prompts, intents, knowledge, APIs, workflow builder, call/conversation review, analytics, users/settings—while reorganising them into a clearer enterprise workflow.

## 1. Non-negotiable Product Direction

1. Rename the product everywhere to **AUREXION EchoSphere**.
2. Replace the old broad multi-step bot-creation experience with a **single VoiceBot Studio workspace**.
3. Keep a short, guided first-time onboarding flow only for initial tenant and first-bot setup.
4. Maintain a strict separation between:
   - **Super Admin**: platform governance, global AI/telephony/security/billing controls.
   - **Tenant Admin**: their own bots, knowledge, workflows, approved voices, testing, publishing, users, and analytics.
5. Do not expose model routing, embedding configuration, hidden system prompts, token limits, raw guardrails, or global provider credentials to Tenant Admins.
6. Make desktop the primary experience. The responsive tablet/mobile experience should support monitoring, approvals, and lightweight administration—not complex drag-and-drop workflow building.
7. Preserve working backend APIs and existing data contracts unless a change is explicitly required. First map the current routes, components, APIs, and data models before changing UI architecture.

## 2. Product Goals

- Make complex AI/VoiceBot configuration understandable to non-technical business users.
- Make the platform look enterprise-grade, governed, auditable, and trustworthy.
- Let a customer complete this path without support:

  `Create Bot → Add Knowledge → Choose Voice → Configure Prompts/Workflow → Test → Review → Publish → Monitor → Improve`

- Allow Super Admins to manage tenants, subscriptions, global governance, health, and platform risk centrally.
- Make every editable item versioned or auditable when it affects bot behavior.

## 3. Existing Product Areas to Preserve and Modernise

The demonstration video indicates existing operational areas such as inventory/list views, bot overview/configuration, prompts, intents, knowledge, APIs, visual workflows, calls/conversation review, analytics, and users/settings. Reuse these functional capabilities, but reorganise the experience into the IA below. Do not discard working business logic merely because the visual layout changes.

## 4. Information Architecture

### 4.1 Super Admin Navigation

- Dashboard
- Tenants
  - Organizations
  - Tenant Onboarding
  - Subscriptions
  - Billing
  - Usage
- AI Governance
  - Approved Models
  - Prompt Library
  - Prompt Versions
  - Knowledge Templates
  - Guardrails
- Voice Platform
  - VoiceBots
  - Phone Numbers
  - SIP & Telephony
  - Channels
- Knowledge
  - Global Knowledge
  - Document Repository
  - URL Repository
  - Embedding Monitor
- Workflows
  - Journey Builder
  - Intents
  - Entities
  - Actions
- Monitoring
  - Platform Health
  - AI Health
  - Telephony Health
  - Alerts
- Security
  - Users
  - Roles
  - Audit Logs
- Reports
  - Usage
  - Revenue
  - AI Cost

### 4.2 Tenant Admin Navigation

- Dashboard
- My VoiceBots
- Knowledge Hub
- Workflows
- Channels
- Analytics
- Conversation Review
- Team
- Integrations
- Settings

Tenant Admin must only see their own tenant data.

## 5. Global UX and Visual System

### 5.1 Application Shell

- Persistent left navigation on desktop with module icons, labels, active state, expandable groups, and tenant switcher only when permitted.
- Top bar: tenant/bot context breadcrumb, global search, alerts, help, profile, and environment/status label.
- Use a contextual right-side panel or drawer for quick detail, activity, approval, audit, and inline help.
- Do not force users to lose their work when moving between pages; preserve filters, tab state, and unsaved-change warnings.

### 5.2 Design Language

- Modern enterprise SaaS: white or very-light neutral background, a clean purple AUREXION accent, high contrast text, restrained shadows, clear data tables, and spacious cards.
- Use a compact 8px spacing system and responsive 12-column desktop grid.
- Use consistent semantic colors:
  - Purple: primary action and active state
  - Green: healthy/success/published
  - Amber: warning/review required
  - Red: failure/risk/destructive action
  - Gray: disabled, historical, no data
- All icons must have labels or accessible tooltips.
- Avoid decorative AI imagery. Use workflow, voice, governance, and data visualisation motifs.

### 5.3 Required Shared Components

- KPI card
- Data table with search, saved filters, sorting, pagination, bulk actions
- Status chip: Draft / Review / Approved / Published / Archived / Failed / Warning
- Health score indicator with drill-down
- Empty state with a clear next action
- Error state with retry and technical detail drawer
- File upload drop zone with queue, validation, progress, and failure reason
- Confirmation modal for destructive actions
- Version history / compare / restore panel
- Activity and audit timeline
- Approval request drawer
- Slide-over detail panel
- Toasts for success/failure; do not use success toasts as the only confirmation for high-impact actions

## 6. Required Role and Permission Model

### Super Admin
Can manage all tenants, global providers, global policy, billing, telephony, platform monitoring, global prompts, templates, and platform audit logs.

### Tenant Admin
Can manage their own tenant configuration, users, bots, knowledge, approved prompts, approved voices, workflows, channels, tests, publishing, and analytics.

### Tenant Roles
- **Bot Manager**: create/configure/test/publish bots within permissions.
- **Knowledge Manager**: upload, edit, re-sync, and review knowledge sources.
- **QA Reviewer**: test scenarios, review calls, flag issues, create improvement recommendations.
- **Analyst**: read analytics and export reports.

Every screen must hide unavailable actions, not merely disable them without explanation.

## 7. Core Experience Workflow

### 7.1 Tenant Onboarding (Super Admin)

Use a short, explicit seven-step wizard only at tenant creation:

1. Company Information
   - company name, industry, country, timezone, primary language, secondary languages, expected call volume
2. Subscription
   - plan, users, bots, minutes, storage, knowledge capacity, API limits
3. Admin User
   - name, email, phone, role, MFA requirement
4. AI Configuration
   - assign approved LLM/STT/TTS profile, knowledge storage policy, approved prompt template group
5. Telephony
   - provider, SIP details, phone numbers, routing, recording policy
6. Security
   - RBAC baseline, SSO, IP allowlist, MFA, audit retention
7. Review & Launch
   - show provision checklist: database/storage/resources/credentials/welcome email

**UX rule:** show validation and an onboarding progress summary. A failed provisioning step must be visible, actionable, and retryable.

### 7.2 VoiceBot Lifecycle

1. Create a bot from blank or template.
2. Assign business objective, owner, languages, channel scope, and initial status = Draft.
3. Open Bot Studio.
4. Configure data and behavior.
5. Test with saved test scenarios.
6. Submit for review.
7. Approve and publish.
8. Monitor live conversations and metrics.
9. Turn insights into a tracked improvement item.
10. Version and republish with rollback support.

## 8. Screen-by-Screen Design Requirements

### 8.1 Super Admin Dashboard

**Purpose:** Platform command center.

**Top KPIs:** Total tenants, active VoiceBots, calls today, revenue, AI cost, platform uptime, open critical alerts.

**Main widgets:**
- Call volume trend
- Top tenants by usage and health
- AI/STT/TTS provider health
- Telephony health
- Cost anomaly chart
- Critical alerts
- Tenant onboarding/provisioning queue
- Quick actions: Create Tenant, View Alerts, Add Provider, Review Usage

**Interactions:** click every KPI and widget to drill into filtered detail, not a dead dashboard card.

### 8.2 Organizations / Tenant Management

**Layout:** searchable table by default plus optional card view. Include tenant name, plan, status, health, user count, bot count, usage, storage, last activity, and actions.

**Actions:** create tenant, suspend/activate, view tenant, usage, billing, impersonation only with clear security warning and audit log.

**Tenant Detail Workspace Tabs:** Overview, Users, VoiceBots, Knowledge, Usage, Billing, Integrations, AI Usage, Deployment History, Audit Logs.

### 8.3 VoiceBot Inventory

**Purpose:** one operational inventory for all bots.

**Required columns/cards:** Bot name, tenant, owner, status, version, channels, languages, health, calls, containment, AI cost, last published, actions.

**Actions:** create, clone, open studio, request publish, publish when permitted, archive, restore, compare versions.

**Design rule:** inventory is not a configuration page. Opening a bot must move into a dedicated studio context.

### 8.4 VoiceBot Studio — Main Workspace

Use a sticky Studio header:
- Bot name, status, version, owner, last saved, unsaved changes, review state
- Primary actions: Save Draft, Run Test, Submit for Review, Publish
- Secondary actions: Clone, Export, Archive, View Audit

Tabs must be:
1. Overview
2. Knowledge
3. Prompts
4. Voice
5. Intents & Entities
6. APIs
7. Workflows
8. Channels
9. Testing
10. Analytics
11. Publish

#### Overview
- Business objective, bot description, primary audience, owner, supported languages, business hours, escalation policy.
- Bot health panel: knowledge health, workflow health, API health, test pass rate, publishing readiness.
- Configuration completion checklist. Clicking a failed item should go to the exact fix location.

#### Knowledge Hub
- Tabs: Documents, URLs, FAQs, Connected Sources.
- Source table: source name, type, status, chunk count, index health, freshness, last sync, owner, actions.
- Upload flow: file validation, duplicate detection, language detection, processing status, failed-page explanation, re-index/re-sync.
- Knowledge quality panel: coverage score, freshness score, retrieval success, missing answers, unused sources, top-used sources.
- Search and preview panel must show source content, relevant chunks, and permissions.
- Advanced retrieval configuration remains Super Admin-only.

#### Prompt Studio
- Tenant-safe prompt types: Greeting, Verification, Escalation, Fallback, Closing.
- For each prompt: editor, variable chips, preview conversation, language variants, save draft, version history, compare, restore.
- Approval states: Draft → Review → Approved → Published.
- Include A/B experiment creation only if backend capability exists; otherwise reserve the UI as future scope.
- Do not show system prompts, model instructions, hidden guardrails, or tool orchestration prompts to tenants.

#### Voice Selection
- Voice marketplace-like layout with cards and filters: language, accent, gender, provider/approved voice family, use case.
- Each card: name, language, accent, gender/presentation label if supplied by provider, sample playback, supported features.
- Selected voice settings: speed, pause duration, empathy, energy, preview text, preview audio.
- Allow language-to-voice mapping, such as English → Voice A, Hindi → Voice B.
- Use an unsaved configuration state and compare before/after preview.

#### Intents & Entities
- Intent library table with confidence threshold, status, sample utterance count, usage, accuracy, last changed.
- Intent detail: description, sample utterances, routing target, fallback behavior, test panel, version history.
- Entities: data type, extraction rules, validation constraints, sample values, PII warning, API mapping.

#### APIs
- API repository with endpoint cards/table: name, purpose, environment, status, last test, response time, owner.
- API builder: method, URL, auth reference (never reveal secret), headers, query params, body template, variables, response mapping, timeout, retry policy.
- Test console: sample input, masked output, status, latency, error details.
- Version and approval controls for all production-impacting API changes.

#### Workflow Builder
- Canvas with left node palette, center workflow canvas, right configuration inspector.
- Nodes: Start, Authentication, Intent, Knowledge, API Call, Condition/Decision, Message/Voice Response, Escalation, Human Transfer, Wait/Callback, End.
- Validate paths before save/publish: no dead-end, missing fallback, unconfigured API, unreachable node, unsupported channel path.
- Use minimap, undo/redo, auto-layout, version history, comments, draft/published markers.
- Standard recommended path:
  `Start → Authenticate → Detect Intent → Retrieve Knowledge / Call API → Decision → Resolve OR Escalate → End/Human Agent`
- Human handover configuration: queue, skill group, business hours, priority, fallback queue, callback behavior, escalation reason.

#### Channels
- Channel cards: Voice, WhatsApp, Web, Mobile.
- Each shows configured status, live status, associated route/workflow, languages, phone number or endpoint, last test, error state.
- Voice: phone number/SIP/routing/recording disclosure.
- WhatsApp: business profile/status/template requirements.
- Web: embed snippet or allowed domains.
- Mobile: SDK/API configuration.

#### Testing Studio
- Split layout: left conversation simulator; right observability/trace panel.
- Show: bot response, intent, confidence, entity values, retrieved knowledge/chunks, API calls, prompt version, latency, token/cost data if tenant is allowed, failures.
- Test scenarios library: policy status, booking, complaint, escalation, API failure, missing knowledge, multi-language, authentication failure.
- Support save scenario, run one test, run regression suite, expected result, pass/fail, comparison with baseline.
- A test result must link directly to the failing prompt, knowledge source, API, workflow node, or intent.

#### Analytics
- KPIs: calls, containment, escalation rate, CSAT, intent accuracy, knowledge health, cost, language distribution.
- Charts: call trend, intent trend, channel trend, language use, top knowledge sources, API failures, escalation reasons.
- Insights panel must be actionable: “Add FAQ”, “Improve workflow”, “Update prompt”, “Fix failing API”, “Review low-confidence intent”.
- All exports must respect role permissions and tenant isolation.

#### Publish Center
- Timeline/status: Draft → Review → Approved → Published → Rolled Back.
- Readiness checklist: required knowledge indexed, workflow valid, channel config valid, test suite passed, approval obtained, no critical alert.
- Show exact version differences in prompts, workflows, knowledge sources, and integrations.
- Publish modal must show impact: channels, rollback version, scheduled time, release notes.
- Rollback must be a first-class, safe action with confirmation and audit event.

### 8.5 Tenant Dashboard

**Purpose:** immediate operational view after login.

Show KPIs: calls today, containment, escalation, CSAT, bot accuracy, knowledge coverage.

Show widgets: call trend, intent trend, language usage, knowledge usage, alerts, improvement opportunities.

Quick actions: Create Bot, Upload Knowledge, Run Test, Review Calls, Publish Changes.

### 8.6 Conversation Review

**Layout:** filter bar + call list + detail pane.

Filters: date, bot, channel, intent, sentiment, escalated, accuracy range, QA status.

Detail panes: recording/playback, transcript, intent/entity details, sentiment timeline, knowledge source/chunks, API trace, escalation reason, QA scorecard, comments, tags, export.

Include recommendation shortcuts: Add FAQ, Update Prompt, Improve Workflow, Open Ticket. Each creates a traceable improvement work item linked to the conversation.

### 8.7 Analytics Dashboard

Differentiate it from Conversation Review: analytics is aggregate and executive; review is conversation-level QA.

Include executive summary, trends, bot comparison, top intents, top knowledge sources, knowledge gaps, channel performance, cost/ROI, customer experience indicators, and recommended actions.

### 8.8 Team Management and Settings

Team: invite user, role assignment, status, last login, ownership assignment, recent activity.

Settings: business hours, languages, branding, notifications, timezone, holiday calendar. Keep provider/model/guardrail settings hidden from tenant admins.

## 9. Critical UX States and Edge Cases

Design every screen for all states below—not only happy paths:

- First-use empty state
- No permission
- Loading/skeleton
- Partial data
- API failure
- Knowledge indexing failure
- File rejected due to format/size/duplicate
- Failed document pages
- Stale source requiring re-sync
- Voice preview unavailable
- API secret missing
- Workflow validation failure
- Publish blocked by failed test or missing approval
- Concurrent edit / outdated version
- Unsaved changes on navigation
- Archived bot
- Suspended tenant
- No call data yet
- Long-running background job
- Audit log unavailable/error

## 10. Accessibility and Quality Requirements

- WCAG 2.1 AA contrast target.
- Keyboard support for all navigation, dialogs, menus, forms, and workflow builder operations.
- Visible focus states.
- Do not rely only on colour for status.
- Use plain-language labels; explain technical terms in contextual help.
- All destructive or production-impacting actions must require confirmation and create an audit record.
- Support English first; build all layouts to support multilingual UI labels later.

## 11. Recommended Build Sequence

### Phase 0 — Audit and Foundation
- Map current frontend routes/components/state management.
- Map current backend endpoints/data entities.
- Build design tokens, shared shell, shared components, role guard layer, and state/error patterns.
- Do not change business logic before documenting dependencies.

### Phase 1 — Core Tenant Experience
- Tenant Dashboard
- My VoiceBots inventory
- Bot Studio shell and Overview
- Knowledge Hub
- Voice Selection
- Basic Team/Settings

### Phase 2 — Bot Behaviour Configuration
- Prompt Studio
- Intents & Entities
- API Repository
- Workflow Builder
- Channels

### Phase 3 — Quality and Release Control
- Testing Studio
- Publish Center
- Versioning/approval/rollback surfaces
- Audit timeline

### Phase 4 — Operations and Improvement
- Conversation Review
- Analytics Dashboard
- Recommendations-to-improvement workflow
- Notifications and alerts

### Phase 5 — Super Admin Platform Controls
- Super Admin Dashboard
- Organizations/Tenant Onboarding
- Platform monitoring
- AI Governance
- Telephony and usage/billing surfaces

## 12. Definition of Done for Every Screen

A screen is complete only when it includes:

1. Desktop layout and responsive behavior.
2. Role/permission behavior.
3. Empty/loading/error/success states.
4. Realistic sample data schema/mocks.
5. Validation and confirmation behavior.
6. Accessibility behavior.
7. Linkage to related screens and workflow context.
8. Audit/versioning behavior where applicable.
9. Developer-ready component breakdown.
10. Acceptance criteria and manual QA cases.

## 13. Required Deliverables from Claude/Codex

Ask the implementation assistant to produce, in this order:

1. Current-state audit of routes, components, APIs, and reusable elements.
2. Proposed IA and routing map, showing old route → new route mapping.
3. Design system/tokens and reusable component inventory.
4. Screen-by-screen UX specification.
5. Figma-style wireframe descriptions or high-fidelity component implementation plan.
6. Implementation plan split into small pull-request-sized tasks.
7. Revised frontend implementation with no regressions to existing APIs.
8. Unit/component tests and end-to-end critical-path tests.
9. Screenshot/test evidence for each redesigned screen.
10. Change log listing every modified file and a rollback note.



