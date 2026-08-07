# Voice Runtime API

API reference for the EchoSphere Voice Runtime service (`voice_runtime/`) —
the process that hosts realtime voice calls. This documents every HTTP and
WebSocket surface the runtime exposes, the exact wire protocol on each, and
the observable diagnostics.

Source of truth: `voice_runtime/app.py` (routes), `voice_runtime/gateway.py`
(gateway entry point), `shared/telephony_webhooks.py` (webhook handler),
`voice_runtime/serializer.py` (browser wire protocol),
`voice_runtime/telephony.py` (telephony media serializers),
`voice_runtime/brain.py` (side-channel JSON events). Deep protocol/deployment
detail for telephony lives in [../TELEPHONY.md](../TELEPHONY.md) and the
Vaani dialer contract in [../VAANI_INTEGRATION.md](../VAANI_INTEGRATION.md);
internal architecture is in [../VOICE_RUNTIME.md](../VOICE_RUNTIME.md).

## Processes, ports and base URLs

**One FastAPI app, two entry points.** `voice_runtime/gateway.py` imports the
exact same `app` object from `voice_runtime/app.py` — the gateway is *not* a
different service, it is the voice-worker app bound to the public dialer port
(plus an eager preload of Pipecat/provider modules so the first telephony
socket after a restart is not starved by lazy imports).

| Process | Run command | Bind | Typical use |
|---|---|---|---|
| Voice Worker | `env/bin/python -m voice_runtime.app` | `VOICE_WORKER_HOST`:`VOICE_WORKER_PORT` (default `0.0.0.0:9002`) | Browser test-console calls (`/ws/voice/...`) |
| Telephony / Vaani Gateway | `env/bin/python -m voice_runtime.gateway` | `TELEPHONY_GATEWAY_HOST`:`TELEPHONY_GATEWAY_PORT` (default `0.0.0.0:9011`) | Dialer webhook + telephony media WS on one public host:port |

Because both processes serve the same app, **all routes below exist on both
ports**. Sessions are handed off through Redis, so any instance can serve a
session it did not mint — in practice the gateway serves the sessions its own
webhook minted (the returned WS URL points at `TELEPHONY_PUBLIC_WS_BASE`),
and browser sessions use the 9002 worker (the backend's session response
carries `workerPort`).

Routes (verified against the live app):

```text
GET   /
GET   /health
POST  /telephony/webhook/{provider}
WS    /ws/voice/{session_id}
WS    /ws/telephony/{provider}/{session_id}
```

FastAPI additionally exposes its generated, public documentation assets at
`GET /docs`, `GET /openapi.json`, `GET /redoc`, and
`GET /docs/oauth2-redirect`. They describe only the three HTTP operations
(OpenAPI does not encode the WebSocket message protocol), are not counted as
Voice Runtime business APIs, and carry no request body or application auth.

The platform API (port 9001) additionally mounts the same webhook handler at
`/api/v1/telephony/webhook/{provider}` (historical path) and issues browser
sessions at `POST /api/v1/voice-sessions`.

### Environment variables (names/purpose only)

| Variable | Purpose |
|---|---|
| `VOICE_WORKER_HOST` / `VOICE_WORKER_PORT` | Bind address of the voice worker (default `0.0.0.0` / `9002`) |
| `VOICE_WORKER_CONCURRENCY` | Max simultaneously active calls per process (default 20); excess connections close with `4429` |
| `TELEPHONY_GATEWAY_HOST` / `TELEPHONY_GATEWAY_PORT` | Bind address of the gateway process (default `0.0.0.0` / `9011`) |
| `TELEPHONY_PUBLIC_WS_BASE` | Public `ws(s)://host:port` base embedded in webhook responses so the dialer can reach the media WS; when unset the webhook derives it from the request URL |
| `TELEPHONY_WEBHOOK_SECRET` | Shared HMAC secret for webhook signatures, referenced via `TELEPHONY_WEBHOOK_SECRET_REFERENCE` (default `env:TELEPHONY_WEBHOOK_SECRET`) |
| `VOICE_SESSION_TIMEOUT` | Redis session TTL **and** the connected transport's absolute session timeout, seconds (default 900) |
| `MAX_CALL_DURATION` | Hard per-call duration timer, seconds (default 3600); the worker cancels the pipeline when it fires |
| `DEFAULT_SILENCE_TIMEOUT` | Base for the pipeline speech-idle monitor, seconds (default 12; idle timeout = 4× this value, logs only — it does not disconnect) |
| `POST_CALL_WORKER_EMBEDDED` | Run the post-call analysis poller inside this process (default true) |
| `VOICE_CALL_RECORDING_ENABLED` / `VOICE_RECORDINGS_DIR` | Toggle and storage directory for per-call stereo WAV recordings |

## Table of contents

- [GET / and GET /health](#get--and-get-health)
- [POST /telephony/webhook/{provider}](#post-telephonywebhookprovider)
- [Session issuance (backend handshake)](#session-issuance-backend-handshake)
- [WS /ws/voice/{session_id}](#ws-wsvoicesession_id-browser-client)
- [WS /ws/telephony/{provider}/{session_id}](#ws-wstelephonyprovidersession_id-telephony-media)
- [WebSocket close codes](#websocket-close-codes)
- [Call lifecycle](#call-lifecycle)
- [Runtime diagnostics](#runtime-diagnostics)

---

## GET / and GET /health

`GET /` — liveness identity:

```json
{"service": "EchoSphere Voice Worker", "status": "up"}
```

`GET /health` — liveness plus dependency check:

```json
{
  "status": "up",
  "active_sessions": 3,
  "redis": {"ok": true}
}
```

`redis` is `{"ok": true}` on a successful `PING`, or
`{"ok": false, "error": "<ExceptionClassName>"}` on failure.
`active_sessions` is the count of live call pipelines in **this** process.

---

## POST /telephony/webhook/{provider}

Signed inbound-call webhook. Mints a single-use voice session and answers
with the provider-specific "connect your media stream here" payload pointing
at `/ws/telephony/{provider}/{session_id}` on this host. Handler:
`shared/telephony_webhooks.py::handle_inbound_call_webhook` (mounted here at
the root path and by the platform API under `/api/v1`).

### Supported providers

`{provider}` must be one of `SUPPORTED_PROVIDERS` (`shared/telephony.py`):

```text
freeswitch | twilio | telnyx | plivo | exotel | vaani
```

Anything else is `404 Unsupported telephony provider '…'`.

### Signature verification

Two schemes, both using the secret resolved from
`TELEPHONY_WEBHOOK_SECRET_REFERENCE`:

| Providers | Scheme |
|---|---|
| `twilio` | `X-Twilio-Signature`: base64 HMAC-SHA1 over `url + sorted(k+v of POST form params)` (Twilio's documented algorithm), constant-time compare. Replay key = signature + `CallSid`. |
| all others | `X-Webhook-Signature` (lower-case hex HMAC-SHA256 of `"<timestamp>." + raw request body`) + `X-Webhook-Timestamp` (Unix seconds, max clock skew ±300 s). |

**Replay protection:** every accepted signature is single-use (Redis `SETNX`
on `webhook:seen:<sha256(signature)>`, 600 s window) — an exact replay is
`403 Webhook replay detected`. A Redis outage on the replay store alone fails
open (the already-verified signature is accepted) with a loud error log.

### Request payload (every field the code reads)

Twilio sends a POST form; the handler reads only `To`, `From`, `CallSid`.
All other providers send JSON (form fallback if the body is not JSON):

```json
{
  "To": "+91 80 4522 1010",
  "From": "+919812345678",
  "callId": "DIALER-CALL-8f3a2b",
  "botId": "<BOT_ID>",
  "variables": {
    "customer_name": "Rohan Sharma",
    "outstanding_amount": "4500"
  }
}
```

| Field | Aliases | Required | Rules |
|---|---|---|---|
| `To` | `to`, `CallTo`, `called_number` | **yes** | The EchoSphere-registered DID; resolved to tenant + default bot via the `phone_numbers` table. Missing → `422 Webhook payload missing the dialed number`. |
| `From` | `from`, `caller_number` | no | Caller number; stored on the session (`caller`) and masked before MySQL. |
| `callId` | `CallSid`, `call_id` | no | Provider call id for trace correlation, stringified and truncated to 64 chars. **Not** an idempotency key. |
| `botId` | `bot_id` | no | Per-campaign bot selection within the DID's tenant. Pattern `^[A-Za-z0-9_-]{1,64}$` (else `422 Invalid botId in webhook payload`); unknown/cross-tenant/unpublished bot → sanitized `404`. |
| `variables` | — | no | Per-call scalar values passed to the bot as reference context. Max 20 keys; key pattern `[A-Za-z0-9_.-]{1,40}`; values (str/int/float/bool) stringified and truncated to 200 chars; invalid entries silently dropped. |

Unknown top-level fields are ignored. The dialed number always anchors the
tenant — no payload field can pick a tenant. Bots whose voice channel has an
explicit disabled `ChannelConfig` row answer `403 This number is not
accepting calls.` (a bot with *no* voice channel row is implicitly enabled).

### Response

The handler creates a Redis voice session (`channel="phone"`, TTL
`VOICE_SESSION_TIMEOUT`) and answers with `connect_instructions()`
(`shared/telephony.py`), embedding
`<TELEPHONY_PUBLIC_WS_BASE>/ws/telephony/{provider}/<SESSION_ID>`:

| Provider | Content type | Body |
|---|---|---|
| `twilio` | `application/xml` | TwiML `<Response><Connect><Stream url="…"/></Connect></Response>` |
| `plivo` | `application/xml` | `<Response><Stream keepCallAlive="true" bidirectional="true" contentType="audio/x-l16;rate=8000">…</Stream></Response>` |
| `telnyx` | `application/json` | `{"stream_url": "…", "stream_track": "inbound_track"}` |
| `exotel` | `application/json` | `{"url": "…"}` |
| `vaani` | `application/json` | `{"url": "…"}` |
| `freeswitch` | `application/json` | `{"audio_stream_url": "…", "audio_fork_url": "…?transport=audio_fork"}` |

Error bodies are JSON `{"success": false, "message": "<reason>"}` (see
[../VAANI_INTEGRATION.md §2.5](../VAANI_INTEGRATION.md) for the full error
matrix).

### Idempotency

- The **signature** is single-use — an exact retry is `403`.
- There is **no `callId` dedup**: a freshly signed retry mints a second live
  session. Dialers must serialize setup attempts and keep the first
  successful URL.
- Duplicate handling for the *media stream* happens at the WebSocket layer
  (close `4409` for a second live connection on the same session).

### Inbound vs outbound

The runtime is direction-agnostic: for both inbound calls and outbound
campaign calls the dialer POSTs this webhook (with `To` = the
EchoSphere-mapped DID, never the customer MSISDN) and then connects the media
WebSocket. Campaign selection rides on `botId`; customer data rides in
`variables` and/or `From`.

---

## Session issuance (backend handshake)

The voice worker **never mints sessions and never decides tenancy**
(`shared/voice_sessions.py`). Sessions come from exactly two doors, both of
which write the trusted mapping into Redis (`voice:session:<SESSION_ID>`, TTL
`VOICE_SESSION_TIMEOUT`, id format `vs_<token>`):

1. **Browser**: `POST /api/v1/voice-sessions` on the platform API
   (`backend/routers/voice_sessions.py`). Authenticated JWT + bot-ownership
   check, body:

   ```json
   {
     "botId": "<BOT_ID>",
     "channel": "browser",
     "variables": {"customer_name": "…"},
     "customerContextId": "<CONTEXT_ID>"
   }
   ```

   `channel` ∈ `browser|phone|sip` (default `browser`); `variables` has the
   same bounds as the webhook's. Response (`201`):

   ```json
   {
     "sessionId": "<SESSION_ID>",
     "botId": "<BOT_ID>",
     "channel": "browser",
     "wsPath": "/ws/voice/<SESSION_ID>",
     "workerPort": 9002,
     "expiresInSeconds": 900
   }
   ```

2. **Telephony**: the signed webhook above.

The Redis session payload the runtime consumes carries `session_id`,
`tenant_id`, `bot_id`, `user_id`, `channel`, `caller`, `call_id`,
`variables`, `customer_context_id`, `created_at`, `status`
(`issued` → `connected` → deleted at call end). The worker re-validates on
connect: unknown/expired id → close `4401`; the session's tenant must match
the resolved bot's tenant → else close `4403`.

---

## WS /ws/voice/{session_id} (browser client)

Realtime voice connection for the browser test client. The **client connects
to the worker**; the opaque `session_id` from `POST /api/v1/voice-sessions`
is the entire authentication (a bearer credential for one call). No headers,
no subprotocol.

Wire protocol (`voice_runtime/serializer.py::RawPCMSerializer`): **binary**
WebSocket messages carry raw PCM audio; **text** messages are small JSON
events. No type declaration is needed — the frame type is the discriminator.

### Audio encoding

| Direction | Format |
|---|---|
| client → server (caller mic) | Binary frames of raw **16-bit signed little-endian mono PCM at 16 000 Hz**. No WAV header, no base64, no JSON envelope. Stream continuously (silence included) — VAD derives end-of-turn from the stream. |
| server → client (bot voice) | Binary frames of raw 16-bit LE mono PCM at the **rate announced in `session_config.sampleRate`** (default 24 000 Hz; per-bot override via `audio_settings.browser.sampleRate`). Never assume a rate client-side. |

### Client → server messages

Only binary audio is consumed. Text frames from the client are currently
**ignored** (`RawPCMSerializer.deserialize` returns `None` for strings); the
shape `{"type": "event", "name": "…"}` is reserved for future use.

### Server → client messages

The first JSON message — sent **before any audio** — is `session_config`;
build the playback pipeline from it. Live-transcript/latency events are sent
as urgent transport messages so they bypass the paced audio queue and arrive
in real time.

| `type` | Fields | When |
|---|---|---|
| `session_config` | `botName`, `sampleRate`, `language`, `languages`, `voices`, `defaultVoice`, `warnings` | Once, at session open, before the greeting audio |
| `partial_transcript` | `text` | Interim STT results (UI only — never become turns or billing) |
| `transcript` | `text`, `at` (ISO-8601) | The final caller utterance of a dispatched turn |
| `bot_text` | `text`, `at` (ISO-8601) | The bot's full reply text (LLM or scripted), as speech starts rendering |
| `language` | `language` | Conversation locale switched (per-turn language following) |
| `event` | `name: "bot_speaking_started"` | First bot audio of a reply hit the wire |
| `event` | `name: "bot_speaking_stopped"` | Bot reply playback finished |
| `event` | `name: "interruption"` | Barge-in: discard buffered bot audio immediately |
| `event` | `name: "language_unsupported"`, `language` | Caller persisted in a language the bot does not serve |
| `event` | `name: "tool_executed"`, `tool`, `ok`, `status`, `error`, `latency_ms`, `mocked` | A verification tool ran during the turn |
| `telephony_control` | `event: "transfer"`, `reason`, optional `transfer_queue`, `agent_id` | Human handoff decided; flushed **after** the announcement finishes playing |
| `error` | `message` | Provider failure as a category code (e.g. `stt_failure:timeout`, `tts_failure:upstream`), max 120 chars — never provider payloads or secrets |

Example `session_config` (values from the bot's resolved config):

```json
{
  "type": "session_config",
  "botName": "Collections Assistant",
  "sampleRate": 24000,
  "language": "hi-IN",
  "languages": ["hi-IN", "en-IN"],
  "voices": {
    "hi-IN": {"provider": "sarvam", "voice": "shubh", "gender": "male"},
    "en-IN": {"provider": "elevenlabs", "voice": "Riya", "gender": "female"}
  },
  "defaultVoice": {"provider": "sarvam", "voice": "shubh", "gender": "male"},
  "warnings": {}
}
```

Example live-turn sequence:

```json
{"type": "partial_transcript", "text": "mujhe payment"}
{"type": "transcript", "text": "mujhe payment karna hai", "at": "2026-08-07T09:15:21.412+00:00"}
{"type": "bot_text", "text": "Bahut accha! Aap UPI se abhi payment kar sakte hain…", "at": "2026-08-07T09:15:23.108+00:00"}
{"type": "event", "name": "bot_speaking_started"}
{"type": "event", "name": "bot_speaking_stopped"}
```

Barge-in from the client's perspective: the client just keeps streaming mic
audio; when the runtime confirms an interruption (word-confirmed while the
bot is speaking — see `voice_runtime/barge_in.py`) it cancels generation and
sends `{"type": "event", "name": "interruption"}`; the client must flush any
buffered bot audio.

### Lifecycle and disconnect

- On connect the session status becomes `connected`; the pipeline starts and
  the **bot speaks first** (greeting, preceded by `session_config`).
- Caller hang-up phrases ("bye", "रखता हूँ", …) are detected
  deterministically per STT segment; the bot speaks a short acknowledgement
  and the worker ends, then closes the socket normally (code 1000).
- A client disconnect cancels the pipeline; the transcript is still
  finalized.
- The transport enforces `VOICE_SESSION_TIMEOUT` as an absolute connected
  timeout (a `session_timeout` voice event is recorded, then the call is
  cancelled); `MAX_CALL_DURATION` is a second hard timer.
- The Redis session is **single-use**: it is deleted at call end. Reconnects
  need a fresh `POST /api/v1/voice-sessions`.

---

## WS /ws/telephony/{provider}/{session_id} (telephony media)

Provider media stream for calls minted by the webhook. `{provider}` must be
in `SUPPORTED_PROVIDERS` (else close `4404`); the session id is validated
against Redis before `accept()`. For `twilio|telnyx|plivo|exotel|vaani` the
worker reads up to **4 JSON text messages within 10 s** waiting for the
stream-start event (`start` / `streamStart` / `media_start`); a missing or
malformed handshake closes `4400`. FreeSWITCH sends no JSON handshake.

Serializer per provider (`voice_runtime/telephony.py::build_media_serializer`):

| Provider | Serializer | Notes |
|---|---|---|
| `vaani` | `VaaniFrameSerializer` | JSON event protocol below; requires `start.mediaFormat` sampleRate 8000 / channels 1 (else `4400`) |
| `twilio` | pipecat `TwilioFrameSerializer` | needs `start.streamSid` (+`callSid`) |
| `telnyx` | pipecat `TelnyxFrameSerializer` | needs `start.stream_id` (+`call_control_id`, outbound encoding from `media_format.encoding`, default PCMU) |
| `plivo` | pipecat `PlivoFrameSerializer` | needs `start.streamId` (+`callId`) |
| `exotel` | pipecat `ExotelFrameSerializer` | needs `start.stream_sid` |
| `freeswitch` | `FreeSwitchAudioStreamSerializer` (default, `mod_audio_stream`) or `FreeSwitchAudioForkSerializer` (`?transport=audio_fork`, `mod_audio_fork`) | binary frames, JSON envelopes outbound — see below |

### Vaani protocol (JSON events)

Full dialer contract, timings, retry semantics, sequence diagrams and error
matrix: [../VAANI_INTEGRATION.md](../VAANI_INTEGRATION.md) (authoritative for
the Vaani team). Summary catalog, verified against
`VaaniFrameSerializer`:

**Audio contract** — `media.payload` is base64 of raw
**8 000 Hz, 16-bit signed little-endian, mono PCM** (`audio/lin`), no WAV
header, no data-URI prefix. Outbound chunks are aligned to **320-byte**
(20 ms) boundaries; steady-state chunks buffer to **3 200 bytes** (200 ms)
with a hard cap of 100 000 bytes (base64 guard 140 000 chars inbound).
To cut response latency, the **first packets of each bot utterance ramp up**
(640 → 1 280 → 2 560 bytes before steady state; the ramp resets after ≥0.5 s
of outbound silence), and the final chunk of an utterance may be shorter.

**Dialer → EchoSphere events:**

| Event | Required | Handling |
|---|---|---|
| `connected` | no | Accepted and ignored |
| `start` | **yes** | Handshake; carries `streamSid` (top level or `start.streamSid`) and `start.mediaFormat` (`sampleRate` must be 8000, `channels` must be 1 when present). Duplicate `start` ignored |
| `media` | yes | Caller audio; fields read: `streamSid`, `media.chunk`, `media.payload` (`media.timestamp` is ignored) |
| `stop` | on hangup | Ends the worker (converted to an internal `EndWorkerFrame(reason="caller_stop")`) even if the socket stays open; `stop.reason` ignored |
| anything else (`dtmf`, `mark`/`marker`, `clear`, `transfer`, `hangup`, `error`, …) | — | Ignored safely |

```json
{"event": "start", "streamSid": "<STREAM_SID>",
 "start": {"track": "inbound", "streamSid": "<STREAM_SID>",
           "mediaFormat": {"encoding": "audio/lin", "sampleRate": 8000, "channels": 1}}}
```

```json
{"event": "media", "streamSid": "<STREAM_SID>",
 "media": {"chunk": 1, "timestamp": "1785417438", "payload": "<base64 PCM>"}}
```

```json
{"event": "stop", "streamSid": "<STREAM_SID>", "stop": {"reason": "callended"}}
```

Idempotency (all in the serializer): events carrying a **foreign
`streamSid`** are dropped; `media` with a **non-increasing numeric `chunk`**
is treated as a retry/replay and dropped (never doubled caller audio, STT
usage or replies); oversized/malformed base64 or JSON is ignored; duplicate
`connected`/`start`/`stop` are harmless. Post-handshake **binary** frames are
accepted as raw 8 kHz PCM but bypass all those checks — dialers must use JSON
`media`.

**EchoSphere → dialer events:**

| Event | Meaning | Fields |
|---|---|---|
| `media` | Bot audio to play | `streamSid`, `media.track` (always `"inbound"`), `media.chunk` (sequential **string** counter), `media.timestamp` (epoch seconds, string), `media.payload` |
| `clear` | Barge-in — flush all buffered bot audio immediately | `streamSid`, `clear.reason` (`"interrupt"`) |
| `transfer` | Hand off to a live agent (sent **after** the announcement finished playing) | `streamSid`, `transfer.reason` (open label, e.g. `workflow_handover`), optional `transfer.transfer_queue`, optional `transfer.agent_id` (reserved, not currently emitted) |
| `stop` | Call ended by the bot/platform — exactly **one** is ever sent and nothing follows it | `streamSid`, `stop.reason` (`"stop"`) |

```json
{"event": "media", "streamSid": "<STREAM_SID>",
 "media": {"track": "inbound", "chunk": "1", "timestamp": "1785417439", "payload": "<base64 PCM>"}}
```

```json
{"event": "clear", "streamSid": "<STREAM_SID>", "clear": {"reason": "interrupt"}}
```

```json
{"event": "transfer", "streamSid": "<STREAM_SID>",
 "transfer": {"reason": "workflow_handover", "transfer_queue": "collections_queue_1"}}
```

```json
{"event": "stop", "streamSid": "<STREAM_SID>", "stop": {"reason": "stop"}}
```

The runtime sends **no** `connected`, `mark`, `dtmf`, transcript, `bot_text`
or application-level `error` events on telephony sockets — audio and the four
control events above only. Fatal setup failures use WebSocket close codes.

### FreeSWITCH protocol

Two wire formats, selected by the `transport` query parameter on the WS URL
(the webhook response returns both URLs):

- **`mod_audio_stream`** (default, no query param or `?transport=audio_stream`):
  inbound binary frames are **stereo interleaved L16 PCM at 8 kHz**
  (first channel = caller/read, second = bot/write; only the caller channel
  feeds VAD/STT). Bot audio goes out in the module's JSON envelope:

  ```json
  {"type": "streamAudio",
   "data": {"audioDataType": "raw", "sampleRate": 8000, "audioData": "<base64 PCM>"}}
  ```

  On barge-in the serializer clears its pending audio and sends
  `{"type": "killAudio"}` so the module drops its own playback buffer
  (up to ~2 s of already-shipped audio); disable via
  `FREESWITCH_SEND_KILL_AUDIO=false` for module builds that reject it.

- **`mod_audio_fork`** (`?transport=audio_fork`): fork started in `mono 16k`
  mode — inbound binary frames are caller-only mono L16 PCM at **16 kHz**
  (this rate also feeds STT directly). Bot audio out:

  ```json
  {"type": "playAudio",
   "data": {"audioContentType": "raw", "sampleRate": 8000, "audioContent": "<base64 PCM>"}}
  ```

  `{"type": "killAudio"}` is always sent on interruption.

Both variants chunk outbound audio on 320-byte boundaries (min 3 200 bytes
steady-state with the same first-packet ramp as Vaani, max 32 000 bytes).
Inbound text frames on FreeSWITCH sockets are treated as module metadata and
ignored (logged once). Call *control* (transfer/hangup on the PBX) is
separate, over ESL — see [../TELEPHONY.md](../TELEPHONY.md).

### Hangup detection and teardown (telephony)

Four independent paths end a telephony call; all converge on the same
teardown:

1. **Dialer `stop` event** → `EndWorkerFrame(reason="caller_stop")` → worker
   drains and the serializer emits its single outbound `stop`.
2. **Caller hang-up phrase** (`detect_hangup`, hi/Hinglish/en, evaluated per
   STT segment before any buffering/LLM): current audio is interrupted
   (`clear`/`killAudio` on the wire), a canned acknowledgement plays, then
   `EndWorkerFrame(reason="caller_hangup_request")`. The do-not-call variant
   (`detect_do_not_call`) additionally stores a durable `do_not_call`
   disposition. After either, `_closing` is set and no later STT event can
   produce a reply.
3. **Policy-approved completion** (the completion evaluator in
   `voice_runtime/call_policy.py` gates every close — a polite goodbye alone
   never completes a call): the goodbye is queued, then
   `EndWorkerFrame(reason="policy_completed")`.
4. **Socket drop / timeouts**: client disconnect cancels the worker;
   `VOICE_SESSION_TIMEOUT` (absolute transport timeout) and
   `MAX_CALL_DURATION` cancel it on expiry.

In every case the runtime finalizes the recorder (transcript/usage
persistence, recording close), deletes the Redis session (the URL is dead —
reconnects need a fresh webhook), and closes the socket idempotently.

---

## WebSocket close codes

Both WS endpoints use the same codes (`voice_runtime/app.py`). Rejections
that happen **before** `accept()` (`4401`, `4404` unknown provider) surface
to many client libraries as a failed HTTP upgrade (HTTP 403) rather than a
close frame.

| Code | Meaning |
|---|---|
| `1000`/`1001` | Normal closure after call end |
| `4400` | Invalid/missing stream-start handshake, or serializer configuration error (bad `mediaFormat`, unsupported FreeSWITCH `transport`, missing stream id) |
| `4401` | Unknown or expired session id |
| `4403` | Session tenant does not match the bot's tenant (defense in depth) |
| `4404` | Unknown provider in the path, or bot configuration unavailable |
| `4409` | A live connection already exists for this session (one session id, one media stream — prevents duplicate pipelines and double billing) |
| `4429` | Voice worker at capacity (`VOICE_WORKER_CONCURRENCY`) |
| `4500` | Voice engine configuration error (pipeline construction failed) |

---

## Call lifecycle

End-to-end flow of one call (trace: `voice_runtime/app.py::_run_call`,
`brain.py`, `call_policy.py`, `recording.py`, `shared/post_call/`):

1. **Session mint** — telephony webhook (signed) or
   `POST /api/v1/voice-sessions` (authenticated) writes the trusted
   tenant/bot mapping to Redis.
2. **WS connect** — worker validates session + concurrency + duplicates,
   claims the session, sets status `connected`, resolves the bot's pinned
   config snapshot (`require_published=True` for non-browser channels).
3. **Context load** — runtime/customer context (tenant-defined schema or
   legacy collection context, matched by `customer_context_id` /
   `variables` / caller phone) and, when the tenant opted in, the caller's
   **previous conversation memory** — all bounded and fail-open.
4. **Pipeline build** — `transport.input() → caller audio gate (adaptive
   noise floor) → Silero VAD → VAD latency probe → STT (Deepgram Flux /
   Sarvam realtime WS / segmented REST) → user-turn controller
   (word-confirmed barge-in) → ConversationBrain → TTS (streaming router or
   segmented) → transport.output() [→ AudioBufferProcessor for recording]`.
5. **Greeting** — bot speaks first: `session_config` (browser), then the
   authored greeting or a generated memory-continuation opening.
6. **Turns** — STT finals are quality-gated (`transcript_gate`: noise,
   sub-word fragments, foreign-language hallucinations rejected), buffered
   per utterance, and dispatched by adaptive endpointing
   (`endpointing.py`: short-reply / complete-sentence windows, late-final
   merge). Hang-up and do-not-call are detected deterministically **per
   segment**, before everything else.
7. **Orchestration** — the router handles deterministic platform commands;
   everything else runs the Goal Engine (one structured decision call under
   the bot's goal policy), falling back to the legacy hybrid pipeline
   (LLM classification → phrase fast path → regex) on any engine failure.
   The **call policy** (`call_policy.py`) enforces state: identity gate,
   blockers, transaction-reference verification, action validation,
   completion evaluation, disposition.
8. **Reply** — workflow step, canned phrase, or streaming LLM reply →
   sentence-aggregated TTS (per-language voice, mid-call language switch,
   provider fallback) → audio on the wire. Barge-in cancels LLM/TTS work,
   drops queued audio and emits `interruption`/`clear`/`killAudio`.
9. **Hangup/teardown** — see the paths above; the worker drains queued
   audio, the telephony serializer emits its single `stop`.
10. **Post-call** — `SessionRecorder.finalize()`: recording WAV closed and
    registered; transcript + events + usage upserted to MongoDB
    (`conversation_transcripts`); a `conversation_sessions` row + per-engine
    usage/billing events written to MySQL (idempotent — a repeated finalize
    never re-bills); call-state written back to the context record. When the
    tenant has `callSummaryEnabled=true` and the call contains turns, a
    post-call analysis job is enqueued (`conversation_memories` row = the job).
    The embedded post-call worker (per process, `POST_CALL_WORKER_EMBEDDED`)
    produces the call summary, outcome and Next Best Action in the background.
    `usePreviousCallSummary=true` independently controls loading stored memory
    on a later call. Finally the Redis session is deleted and the socket closed.

---

## Runtime diagnostics

What an integrator can observe without touching the audio path:

- **`GET /health`** — process liveness, `active_sessions`, Redis health.
- **Per-turn latency spans** (`voice_runtime/turn_metrics.py`) — one
  `TurnLatencyTracker` per call measures, in ms: `bot_stop_to_speech`,
  `speech`, `stt_eager_eot`, `stt_final`, `endpoint` (turn-detection dead
  time), `classify`, `tool`, `llm_ttft`, `llm_first_token`, `tts_queue`,
  `tts_ttfb`, `playout`, `tts_first_audio`, `response` (what the caller
  feels). Negative spans are dropped and counted, never clamped. Each
  completed turn logs two structured lines —
  `turn[<SESSION_ID>] latency endpoint=…ms llm_first_token=…ms …` and
  `turn_timing {…}` (one JSON object with absolute epoch-ms timestamps for
  every pipeline boundary, `spans_ms`, and `slowest_stage`) — and both are
  stored as `turn_latency` / `turn_timing` events.
- **MongoDB `voice_events`** — operational events flushed live during the
  call (`SessionRecorder.flush_event`), each
  `{session_id, tenant_id, bot_id, kind, at, data}`. Kinds emitted by the
  runtime include: `call_started`, `session_timeout`,
  `pipeline_build_failed`, `customer_context_loaded`,
  `runtime_context_loaded`, `previous_memory_loaded`, `barge_in`,
  `call_control` (hangup/do_not_call/slower), `handoff`, `tool_executed`,
  `language_detected`, `language_unsupported`, `workflow_off_script`,
  `workflow_reply_language_adapted`, `call_completed_by_policy`,
  `post_hangup_transcript_dropped`, `stt_segment_held_during_bot_audio`,
  `caller_audio_gate` (gate statistics at teardown), `turn_latency`,
  `turn_timing`. Buffered (non-flushed) events land in the transcript
  document's `events` array at finalize.
- **MongoDB `conversation_transcripts`** — the per-call document: turns
  (PII-masked), events, usage (`stt_seconds`, `llm_*_tokens`,
  `tts_characters`, per-engine `tts_usage`), `end_reason`, `disposition`,
  `language`, recording metadata, prompt version.
- **MySQL `conversation_sessions` + usage events** — billing-grade rollup
  per call (duration, containment/escalation, cost).
- **Recordings** — stereo WAV (caller left, bot right) under
  `VOICE_RECORDINGS_DIR/<tenant_id>/<SESSION_ID>.wav` when
  `VOICE_CALL_RECORDING_ENABLED` is on.
- **Process logs** — per-session lifecycle lines
  (`voice session <SESSION_ID> ended (turns=N)`), inbound-audio level
  telemetry on FreeSWITCH sockets (5 s intervals), STT/TTS provider failures
  as category codes, and `telephony.inbound …` routing lines at webhook time
  (provider, dialed number, tenant/bot, session id, `call_id`).
