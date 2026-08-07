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
   structured LLM call per turn (default engine `gpt-4o-mini`, hard 1.2 s budget)
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
| Goal Engine decision budget | hard deadline in `GoalEngine.decide` | 1.2 s |

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
| STT | `sarvam` — streaming WebSocket (`voice_runtime/sarvam_stt.py`, honest segment finalization); `deepgram` — Flux `v2/listen` (`voice_runtime/deepgram_stt.py`, authoritative end-of-turn, per-turn language hints) | any other configured provider (`openai`/`whisper`, `assemblyai`, `mock`) runs as VAD-segmented REST via `EchoSTTService` (`voice_runtime/services.py`) |
| TTS | `sarvam`, `elevenlabs` — persistent WebSocket engines behind `StreamingTTSRouter` (`voice_runtime/tts_router.py`): per-language voice mapping, sentence-aware buffering, delivery params (speed/pause/empathy/energy), barge-in cancellation, fallback to a configured secondary engine on transient failures only | REST providers (`openai`, `azure`, `google`, `mock`) run via `EchoTTSService` |
| LLM | streamed through the shared registry: `openai`, `anthropic`, `google`, `mock` (`shared/providers/factory.py`, lazy SDK imports) | the Goal Engine's orchestration model is configurable per bot (`llm_settings.orchestration_model`, default `gpt-4o-mini`) |

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
  authorized `GET /api/v1/conversations/{id}/recording` endpoint.
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
