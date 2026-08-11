# Voice Runtime

The voice runtime hosts realtime calls in a dedicated process
(`voice_runtime/app.py`, `VOICE_WORKER_PORT`, currently 9002) built on **Pipecat 1.5**.
It is deliberately separate from the HTTP API: a failure inside one call is contained
to that call's pipeline task, and the API process never blocks on audio.

Run: `env/bin/python -m voice_runtime.app` (reads `VOICE_WORKER_HOST/PORT` from `.env`)

The **same FastAPI app** also runs as the telephony gateway
(`env/bin/python -m voice_runtime.gateway`, `TELEPHONY_GATEWAY_PORT`, currently 9011)
so external dialers such as Vaani get one public host:port for both the inbound-call
webhook and the media WebSocket. Routes on both processes: `GET /`, `GET /health`,
`POST /telephony/webhook/{provider}`, `WS /ws/voice/{session_id}`,
`WS /ws/telephony/{provider}/{session_id}`. Full endpoint, close-code and wire-protocol
reference: [api/VOICE_RUNTIME_API.md](./api/VOICE_RUNTIME_API.md).

## Session issuance (trust boundary)

Clients never tell the voice worker who they are.

1. **Browser**: `POST /api/v1/voice-sessions` (`backend/routers/voice_sessions.py`)
   authenticates the JWT, asserts bot ownership, then writes a trusted
   tenant/bot mapping to Redis (`voice:session:{id}`, TTL = `VOICE_SESSION_TIMEOUT`,
   default 900 s) via `create_voice_session` (`shared/voice_sessions.py`).
   The response carries only the opaque `sessionId` and `wsPath`.
2. **Telephony**: the signed inbound-call webhook issues the session the same way —
   either the Platform API's telephony webhook or the runtime's own
   `POST /telephony/webhook/{provider}` (`shared/telephony_webhooks.py`), which
   returns the `/ws/telephony/...` media URL (see [TELEPHONY.md](TELEPHONY.md) and
   [VAANI_INTEGRATION.md](VAANI_INTEGRATION.md)).
3. The worker (`/ws/voice/{session_id}` or `/ws/telephony/{provider}/{session_id}`)
   loads the mapping with `load_voice_session`; unknown/expired ids are closed with
   code 4401 before any pipeline is built.

Non-browser channels require a **published release**: `resolve_bot_config(...,
require_published=True)` refuses bots without one (`shared/bot_config.py`).
The resolved config snapshot is pinned per call — config edits never mutate an active
call. Snapshots are cached in Redis under `botcfg:*` (TTL 300 s) and invalidated on
voice-settings save (`backend/routers/bots.py`) and release publish/rollback
(`backend/routers/releases.py`). If the tenant's `use_previous_call_summary` flag is
on, the customer's previous-call memory (`shared/post_call/recall.py`) is loaded at
connect time and handed to the brain as greeting/continuation context.

## Pipeline

Assembled per call by `build_voice_pipeline` (`voice_runtime/pipeline.py`):

```
transport.input()
  ─► caller audio gate      adaptive energy vs. this call's measured noise floor
     (audio_gate.py)        — suppressed noise can never start a turn or reach STT
  ─► Silero VAD             neural speech probability (VADProcessor)
  ─► STT service            per bot config, see Providers below
  ─► transcript gate        drops noise / sub-word fragments / foreign-language
     (transcript_gate.py)   hallucinations using provider quality metadata + script
  ─► UserTurnProcessor      start: VAD (word-confirmed while the bot speaks),
                            stop: speech timeout from the bot's turn-detection config
  ─► ConversationBrain      routing, Goal Engine, RAG, streaming (brain.py)
  ─► TTS                    StreamingTTSRouter or segmented EchoTTSService
  ─► transport.output()
```

- Audio in at 16 kHz, TTS out at 24 kHz for browser calls (`PipelineParams`);
  telephony transports negotiate the provider's rate (e.g. 8 kHz μ-law/PCM —
  see [TELEPHONY.md](TELEPHONY.md)).
- An `AudioBufferProcessor` at the end of the pipeline feeds the per-call
  **recording** (stereo WAV, caller left / bot right) when recording is enabled.
- Provider failures become `ErrorFrame`s (`stt_failure:*` / `tts_failure:*`), never
  crashes; fatal TTS failures tear the call down instead of playing dead air.

## Turn taking and endpointing

STT transcripts are final per **speech segment**, not per utterance — a caller
pausing mid-sentence produces several finals for one thought. The brain therefore
**buffers segments** and runs the turn when the turn controller signals real
end-of-turn. Late finals are merged: a straggler landing while the reply for a
partial utterance is already generating cancels it, rewinds the partial user turn
and re-runs the combined utterance — one utterance, one LLM turn.

Endpointing is adaptive rather than one fixed silence window
(`voice_runtime/endpointing.py`, config contract in `shared/turn_detection.py` with
per-channel defaults — browser vs. telephony — and validated bounds; stored per bot
in `stt_settings.turn_detection`):

- `user_speech_timeout` — the normal pause window (defaults: 1.2 s browser,
  0.7 s telephony);
- `complete_endpoint` — a much shorter endpoint used when the buffered text reads
  as a finished thought;
- `short_reply_endpoint` — shorter still for self-contained short replies
  ("haan", "ok", "ठीक है");
- `finalize_grace` / `finalize_settle` — debounce for stragglers, skipped when the
  newest final is already stale;
- `barge_in_min_words` — while the bot is **speaking**, VAD alone cannot interrupt
  it; the turn (and the interruption) fires only once the STT has transcribed at
  least this many words (`voice_runtime/barge_in.py`). This is what stopped
  background chatter from chopping replies mid-word. While the bot is quiet, VAD
  starts the turn immediately as before.

With Deepgram Flux the provider's own end-of-turn decision is authoritative and
replaces the local timeout logic (`voice_runtime/deepgram_stt.py`).

Per-turn latency is measured end-to-end (`voice_runtime/turn_metrics.py`): caller
speech duration, STT finalization, turn-detection dead time, LLM first token, TTS
first audio, and the total the caller actually feels.

## ConversationBrain

`voice_runtime/brain.py` sits between STT and TTS. Understanding is
**decision-first** with deterministic guards around it:

1. **Deterministic, before anything else** — hang-up requests are detected on every
   segment (`detect_hangup`, hi/hinglish/en, negation-guarded): audio is
   interrupted, a short acknowledgement plays, the call ends. Do-not-call/consent
   revocation, platform commands (repeat/slower/transfer) and deterministic fast
   paths resolve without paying any LLM latency. A safety rule fires when a caller
   reads out card numbers/OTPs/passwords.
2. **Goal Engine decision** (`shared/orchestration/goal_engine.py`) — one bounded,
   structured LLM call per turn (default engine `gpt-4o-mini`, 1.2 s default
   budget; per-bot `llmSettings.orchestration_timeout_seconds` is clamped to
   0.5–5.0 s)
   produces a validated `ConversationDecision`: intent, generic signal,
   identity/gate outcome, scope (including prompt-injection attempts, which are
   forced onto a redirect and stripped of tool/slot effects), slot observations and
   the next action. On timeout or failure it returns `None` and the turn falls back
   to the deterministic `TurnRouter` path (`shared/orchestration/router.py`:
   workflow → call-control → handoff → smalltalk → configured intents → KB-signal
   heuristics → clarify/chat).
3. **Guarded transitions** — the decision drives the call policy
   (`voice_runtime/call_policy.py`: identity confirmation, commitments, callback
   capture), the LangGraph `WorkflowEngine` for stateful flows, or KB retrieval.
4. **Retrieval** for knowledge routes via `KnowledgeService.search` (direct call —
   no MCP hop): hybrid pgvector + FTS with a confidence gate; retrieved chunks are
   sanitized, quoted with numbered citations, and the system prompt instructs the
   model to treat them as data. Non-answerable retrievals get an honest
   "I couldn't find that…" fallback with a handoff offer.
5. **Streaming** — response-LLM tokens flow downstream as `TextFrame`s between
   `LLMFullResponseStart/EndFrame`; sentence aggregation is Indic-script aware
   (`shared/audio/text.py`).
6. **Recording** — `TurnRecord`s (route, kb_sources, latency_ms) and events
   (`route_decision`, `kb_retrieval`, `generation_cancelled`, `handoff`,
   `call_control`, `barge_in`, …) plus STT/TTS usage counters.

**Barge-in**: on `InterruptionFrame` / `UserStartedSpeakingFrame` the brain cancels
its in-flight generation (retrieval + LLM stream) immediately and flushes a
`generation_cancelled` event; the streaming TTS router additionally cancels
provider-side synthesis and rejects audio for cancelled generations.

Conversation history is capped at 20 turns (`_HISTORY_MAX_TURNS`).

## Timeouts and limits

| Limit | Source | Default |
|---|---|---|
| Max call duration | `MAX_CALL_DURATION` → `worker.cancel()` timer in `voice_runtime.app._run_call` | 3600 s |
| Session (transport) timeout | `VOICE_SESSION_TIMEOUT` → `FastAPIWebsocketParams.session_timeout` | 900 s |
| Idle timeout | `silence_timeout * 4` → `PipelineWorker(idle_timeout_secs=...)` | 48 s |
| Worker concurrency | `VOICE_WORKER_CONCURRENCY` (WS closed 4429 at capacity) | 20 |
| Goal Engine decision budget | `llmSettings.orchestration_timeout_seconds`, clamped by `GoalEngine` | 1.2 s (allowed 0.5–5.0 s) |

## Providers

Per-bot selection lives in the `voice_bot_settings` JSON columns
(`stt_settings` / `tts_settings` / `llm_settings`) and is validated against the
**database-driven provider catalog** (`provider_defs`, `provider_models`,
`voice_profiles`, `supported_languages` — `backend/core/provider_catalog.py`), which
is the single source of truth for valid provider/model/language/voice combinations.
Credentials are secret *references* resolved server-side; raw keys never appear in
API responses. `GET /api/v1/providers/voice-catalog`
(`backend/routers/telephony.py`) exposes the catalog (minus mocks) to the studio UI.
See [VOICE_PROVIDERS.md](VOICE_PROVIDERS.md).

How each kind is wired into the pipeline:

| Kind | Realtime path | Notes |
|---|---|---|
| STT | Governed live providers are `sarvam` (streaming WebSocket) and `deepgram` Flux `v2/listen` (authoritative end-of-turn, per-turn language hints). | Default `sarvam/saaras:v3`. Dormant registry adapters (OpenAI/Whisper, AssemblyAI) are not selectable while their catalog rows are inactive; `mock` is dev/test only. |
| TTS | Governed live providers are `sarvam` and `elevenlabs`, routed through `StreamingTTSRouter`; non-streaming Eleven v3 uses segmented REST. | Default `sarvam/bulbul:v3/shubh`. Dormant OpenAI/Azure/Google adapters do not bypass catalog governance; `mock` is dev/test only. |
| LLM | Governed live provider is `openai`, streamed through the shared registry. | Default `gpt-4o-mini`; the Goal Engine's provider/model, timeout, and token cap are configurable in `llm_settings`. Dormant Anthropic/Google adapters are not selectable while inactive. |

`shared/providers/factory.py` is the adapter registry, but it is not the
availability list. Save-time validation and `resolve_bot_config` require an
active provider and active model in the database catalog; primary engines fail
closed instead of silently switching to an inactive adapter.

## Guardrail enforcement

Every call loads the tenant's **effective guardrails** at call start
(`_run_call` → `shared/guardrails/loader.py`): the mandatory platform rules
(PII redaction, secret-leakage prevention, unsafe-tool blocking,
prompt-injection protection — `guardrails.is_mandatory`, can never be
disabled) ∪ the rules linked to the tenant's assigned guardrail profile
(`tenants.guardrail_profile_id`; profiles managed under
`/api/v1/guardrail-profiles`). The loader FAILS CLOSED: a broken control-plane
lookup degrades to the built-in mandatory floor, never to "no guardrails".
A deactivated profile keeps enforcing for tenants already assigned to it.

The per-call `GuardrailEngine` (`shared/guardrails/engine.py`) enforces
deterministically — pattern checks no model output can bypass:

- **Caller input** (`_handle_turn`, before understanding/tools/LLM): a spoken
  card number under the payment restriction blocks the turn; injection
  attempts are flagged (scope rules already redirect them).
- **Assistant output**: fixed phrases (`_say`) are checked before any audio
  renders; when a blocking output rule is active (medical advice / payment
  credential requests) the LLM stream switches to sentence-hold mode — each
  sentence is checked before it is forwarded to TTS, so a violating sentence
  is never synthesized. Blocked turns speak a localized safe reply
  (en/hi via `shared/orchestration/phrases.resolve_phrase`).
- **Tool calls**: `ToolExecutor.execute` denies every tool call in a turn
  where a blocking guardrail fired (mandatory `unsafe_tool_call_block`).
  Workflow `api` nodes are covered through the session-engine registry
  (`register_session_engine`), so the gate holds without threading objects
  through checkpointed workflow state.
- **Persistence**: transcript turns are redacted through the engine
  (PII + credential patterns) at finalize; `install_log_redaction()` is
  installed in both processes so provider credentials cannot reach logs.

Every hit is recorded (rule code, action, stage, profile id + version,
policy code + version, outcome, non-sensitive detail — never the matched
value): buffered as `guardrail_trigger` events on the transcript, flushed
immediately for blocks, and written to the tenant-scoped MySQL
`guardrail_triggers` ledger at finalize (`GET /api/v1/guardrail-triggers`).
The chat runtime (`POST /bots/{id}/testing/chat`) runs the same
input/output/tool enforcement and persists its triggers per turn.

**Bot-level profiles.** Profile resolution is hierarchical: mandatory
platform rules always apply; a bot with an explicit
`voice_bots.guardrail_profile_id` uses that profile; a NULL column inherits
the tenant default (`tenants.guardrail_profile_id`) — so tenant-default
changes follow inheriting bots and never touch explicit assignments.
Assignment: `PATCH /api/v1/bots/{id}/guardrail-profile` (Super Admin,
audited, active-only for new assignments, `""` → inherit); the effective
result (inherited/explicit flag, rules, active compliance-policy versions):
`GET /api/v1/bots/{id}/effective-guardrails`. The seeded
`development` profile (rules `outbound_call_block`,
`state_changing_tool_block`) is for genuinely internal bots: telephony calls
are refused pre-connect and real state-changing tools are denied at the
executor (mocked Testing Studio runs still work).

## Compliance policies (collections)

`compliance_policies` rows (managed under `/api/v1/compliance-policies`,
Super Admin) carry versioned, regulator-attributed policy data: IANA
timezone, permitted calling windows, per-day contact limits, prohibited
conduct pattern sets, waiver-authorization rules and immutable legal wording
templates, each with primary-source references and approval metadata. Only
`status='active'` policies whose effective date has arrived enforce; drafts
never gate a call, and activation requires a compliance-owner approval note
(`draft → approved → active → retired`; activating a version retires the
previous one).

Enforcement points:

- **Pre-dial** (`shared/telephony_webhooks.enforce_pre_call_compliance`,
  called by the signed webhook before connect instructions are returned, and
  re-checked at `_run_call` before the pipeline starts): calling windows are
  evaluated in the policy timezone via `zoneinfo` (DST-safe, injectable
  clock), per-day contact limits count atomically in Redis under a hashed
  caller key, and the development profile's `outbound_call_block` refuses
  telephony entirely. Refusals are sanitized 403s plus a ledger row with the
  policy code/version — the LLM is never consulted.
- **Output** (`GuardrailEngine`): policy conduct rules (threats, third-party
  disclosure, misleading representations…) and waiver rules become
  data-driven pre-TTS blocks riding the sentence-hold stream; a
  waiver/discount/settlement promise is blocked and escalated
  (`guardrail_waiver` reply) unless a tool-verified authorization
  (`waiver_approved` + `approval_reference`, optional expiry/limit) was
  recorded THIS call via `record_waiver_authorization` — prompt text can
  never authorize.
- **Legal wording**: authored content references `{{wording:code}}`; the
  approved template substitutes VERBATIM in the fixed-phrase speech path
  (never through generation — a workflow step carrying a wording reference
  skips language-adaptation), and the exact template version spoken is
  recorded as an `emitted` trigger.

## Persistence

`SessionRecorder` (`voice_runtime/recording.py`) accumulates turns/events/usage
in memory; single events that matter operationally (barge-in, handoff, timeouts) are
flushed immediately to Mongo `voice_events`. `finalize()` runs once at call end:

- Mongo `conversation_transcripts` upsert — turns (PII-masked: card/aadhaar/PAN),
  events, usage, bot version, recording metadata.
- MySQL `conversation_sessions` row — duration, channel, containment/escalation,
  caller phone masked — plus per-provider `usage_events` for billing.
- Call **recording**: stereo WAV (caller left / bot right) under
  `VOICE_RECORDINGS_DIR` (default `storage/recordings/`), served back by the
  authorized `GET /api/v1/conversations/{conversation_id}/recording` endpoint.
- **Post-call analysis enqueue** (`shared/post_call/processor.py`): if the tenant's
  `call_summary_enabled` flag is on (fail-closed), a queued `conversation_memories`
  row is inserted and the embedded background worker analyzes the call (summary,
  outcome, commitments, Next Best Action). See the memory flow in
  [ARCHITECTURE.md](ARCHITECTURE.md).

Neither write is on the audio critical path, and persistence failures never break the
call.

## Browser test protocol

`RawPCMSerializer` (`voice_runtime/serializer.py`): binary WS frames carry raw
16-bit mono PCM (16 kHz in, output rate out); JSON text frames carry side-channel
events (`transcript`, `bot_text`, `bot_speaking_started/stopped`) so the studio
Testing tab (`src/pages/tenant/studio/TestingTab.tsx`) renders live transcripts.
Telephony streams swap in the provider's serializer instead
(`voice_runtime/telephony.py` for FreeSWITCH/Vaani, Pipecat serializers for
Twilio/Telnyx/Plivo/Exotel — see [TELEPHONY.md](TELEPHONY.md)). The full wire
protocol is documented in [api/VOICE_RUNTIME_API.md](./api/VOICE_RUNTIME_API.md).
