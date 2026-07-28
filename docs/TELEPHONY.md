# Telephony Integration

EchoSphere answers inbound calls from six providers — **Twilio, Telnyx, Plivo,
Exotel, Vaani and FreeSWITCH** (`SUPPORTED_PROVIDERS`, `shared/telephony.py`).
The control flow is: signed webhook → number-to-bot routing → voice session →
provider-specific connect payload → media WebSocket into the voice worker.

External provider accounts are still required for live carrier traffic; the adapters
validate configuration and produce real connect payloads, and provider-mocked tests
cover signatures and routing (`tests/unit/test_webhook_verification.py`).

## Inbound call flow

```mermaid
sequenceDiagram
    participant P as Provider
    participant API as POST /api/v1/telephony/webhook/{provider}
    participant R as Redis
    participant VW as Voice worker /ws/telephony/{provider}/{session_id}

    P->>API: inbound-call webhook (signed)
    API->>API: verify signature + replay protection
    API->>API: dialed number → phone_numbers (status=assigned) → bot
    API->>API: resolve published bot config (require_published=True)
    API->>R: create voice session (trusted tenant/bot mapping)
    API-->>P: connect payload (TwiML / Stream XML / JSON)
    P->>VW: media WebSocket + stream-start handshake
    VW->>R: load session, pin config snapshot
    VW->>VW: build serializer + Pipecat pipeline, run call
```

Implementation: `backend/routers/telephony.py` (webhook),
`voice_runtime/app.py` (`telephony_session`), `shared/telephony.py`
(provider catalog + connect payloads), `voice_runtime/telephony.py`
(media-stream serializers).

## Webhook signature verification

Two schemes (`backend/telephony/webhooks.py`):

| Provider | Scheme |
|---|---|
| Twilio | `X-Twilio-Signature`: HMAC-SHA1 over URL + sorted POST params, base64 (Twilio's documented algorithm), constant-time compare |
| Telnyx / Plivo / Exotel / Vaani / FreeSWITCH bridges | `X-Webhook-Signature` + `X-Webhook-Timestamp`: HMAC-SHA256 over `<timestamp>.<raw body>`, 300 s max clock skew |

**Replay protection**: accepted signatures are single-use within their validity
window (Redis `SETNX` on `webhook:seen:<sha256(signature)>`). A Redis outage fails
open but logs loudly. The shared secret comes from
`TELEPHONY_WEBHOOK_SECRET_REFERENCE` (an `env:` reference — see
[SECURITY.md](SECURITY.md)); for Twilio the same reference resolves to the account
auth token used in the signature.

## Number → bot routing

The dialed number (`To`/`to`/`CallTo`/`called_number` in the payload) is looked up in
MySQL `phone_numbers` with `status='assigned'`
(`resolve_bot_for_phone_number`, `shared/bot_config.py`). The bot must
have a **published release** (`require_published=True`) — draft bots never answer
carrier traffic. The session created for the call is channel `phone` and records the
caller number (masked before it reaches MySQL).

## Connect payloads

`connect_instructions()` returns what the webhook answers with, pointing the
provider's media stream at
`wss://<public_ws_base>/ws/telephony/{provider}/{session_id}`:

| Provider | Payload |
|---|---|
| Twilio | TwiML `<Response><Connect><Stream url="..."/></Connect></Response>` |
| Plivo | `<Response><Stream keepCallAlive="true" bidirectional="true" contentType="audio/x-l16;rate=8000">…</Stream></Response>` |
| Telnyx | JSON `{"stream_url": ..., "stream_track": "inbound_track"}` (for the TeXML/Call Control handler) |
| Exotel | JSON `{"url": ...}` (Voicebot applet) |
| Vaani | JSON `{"url": ...}` (bidirectional VoiceBOT WebSocket endpoint) |
| FreeSWITCH | JSON `{"audio_fork_url": ...}` (consumed by the dialplan helper) |

`public_ws_base` comes from `TELEPHONY_PUBLIC_WS_BASE` when set (required when
the voice worker is not reachable on the host:port that served the webhook —
separate port/host with no proxy in front); otherwise it is derived from the
webhook request's base URL. It must be a publicly reachable `wss://` host in
production.

## Media WebSocket handshake

`/ws/telephony/{provider}/{session_id}` (`voice_runtime/app.py`):

1. The session id is validated against Redis (unknown/expired → close 4401).
2. For twilio/telnyx/plivo/exotel/vaani the worker reads up to 4 JSON messages until the
   provider's stream-start event (`start`/`streamStart`/`media_start`) arrives —
   these serializers need stream identifiers (missing → close 4400).
3. `build_media_serializer()` constructs the Pipecat serializer:
   `TwilioFrameSerializer` (streamSid/callSid), `TelnyxFrameSerializer`
   (stream_id/call_control_id/outbound encoding), `PlivoFrameSerializer`,
   `ExotelFrameSerializer`, `VaaniFrameSerializer` — or
   `RawPCMSerializer(input_sample_rate=8000)` for FreeSWITCH.
4. The regular voice pipeline runs (see [VOICE_RUNTIME.md](VOICE_RUNTIME.md)); the
   channel recorded on the transcript is the provider name.

## Vaani

Vaani Telephony connects to `/ws/telephony/vaani/{session_id}` over `wss://` and
uses JSON events with base64 encoded `8 kHz`, `16-bit` signed little-endian,
mono PCM:

- Vaani → EchoSphere: `connected`, then `start` with `streamSid` and
  `mediaFormat`, then `media` chunks (sequential `chunk` numbers, epoch
  `timestamp`), and finally `stop`.
- EchoSphere → Vaani: `media` chunks for bot audio, `clear` on interruption,
  `transfer` on human handoff, and `stop` at call termination.
- Bot audio is emitted on 320-byte boundaries, targeting at least 3.2 KB per
  chunk and never more than 100 KB. A final short remainder is flushed when bot
  speech stops; exactly one `stop` is ever sent and nothing follows it.

The `start.mediaFormat.sampleRate` must be `8000` and `channels` must be `1`;
invalid Vaani handshakes are rejected with close code `4400`.

**Session lifecycle.** The `session_id` in the WebSocket path is minted by the
signed webhook and is **single-use**: when the socket disconnects (or `stop`
arrives) the pipeline tears down and the session is deleted. A Vaani reconnect
(the old "exponential backoff" guidance) must therefore go through a **fresh
webhook → fresh session**, never re-attach to the old URL — reconnecting to a
dead session is rejected at the WebSocket upgrade (HTTP 403, the socket is
never accepted), and a second live connection for the same session is closed
with `4409` before the stream handshake (one session, one media stream;
prevents duplicate pipelines and double billing). The stream handshake itself
has a 10 s deadline. Live-call audio is never replayed.

**Idempotency.** Inbound events with a foreign `streamSid` are dropped; `media`
events with a non-increasing `chunk` sequence are treated as retries/replays
and dropped (they can't double caller audio, STT usage or bot replies);
payloads over 100 KB, malformed JSON and invalid base64 are ignored; duplicate
`start`/`connected`/`stop` events are safe.

**Transfer.** Human handoff (explicit caller request, escalation intents, or a
workflow `handover` node — whose configured `queue` becomes `transfer_queue`)
emits the `transfer` event *after* the bot has finished speaking its
announcement, so the caller hears it before Vaani re-routes the call.

**Call metadata.** The signed Vaani webhook may include `callId` (logged and
attached to the session for trace correlation) and a `variables` object of
per-call dialer values (max 20 keys, `[A-Za-z0-9_.-]{1,40}` names, values
capped at 200 chars). Variables enter the LLM system prompt as reference data
("Call context") — they are never treated as instructions and never override
tenant/bot resolution, which always comes from the dialed number mapping.

## FreeSWITCH

Two integration surfaces (`voice_runtime/freeswitch.py`):

- **Media**: the dialplan attaches `mod_audio_fork` to the worker's
  `/ws/telephony/freeswitch/{session_id}` endpoint — raw L16 @ 8 kHz both ways, no
  JSON handshake, handled by `RawPCMSerializer`.
- **Control**: `ESLClient`, a minimal asyncio Event Socket Layer client (inbound
  mode) implementing `transfer` (`uuid_transfer`), `hangup` (`uuid_kill`),
  `api`, and `health_check`. Configuration: `FREESWITCH_HOST` (default 127.0.0.1),
  `FREESWITCH_PORT` (default 9004), `FREESWITCH_PASSWORD_REFERENCE`
  (`env:FREESWITCH_PASSWORD`). Every operation **fails loudly** when unconfigured or
  rejected (`ProviderError`) — nothing fakes success.

Deployment notes for the ESL socket are in
[DEPLOYMENT.md](DEPLOYMENT.md#4-freeswitch-esl-notes).

## Failure codes (worker WebSocket)

| Close code | Meaning |
|---|---|
| 4400 | invalid/missing stream handshake or serializer config error |
| 4401 | unknown or expired session id |
| 4403 | session tenant does not match bot tenant (defense in depth) |
| 4404 | unknown provider, or bot configuration unavailable |
| 4429 | voice worker at capacity (`VOICE_WORKER_CONCURRENCY`) |
