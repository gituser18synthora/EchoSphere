# Voice Provider Configuration (Sarvam AI · OpenAI · ElevenLabs · Deepgram)

The realtime voice stack is fully database-driven: which providers, models,
languages, voices and parameters a Voice Bot may use comes from the provider
catalog tables, never from hardcoded lists.

## Credentials

Set in the root `.env` (shared by the API and the voice runtime — the file is
loaded into the process environment at startup):

```env
OPENAI_API_KEY=<OPENAI_API_KEY>
SARVAM_API_KEY=<SARVAM_API_KEY>
ELEVENLABS_API_KEY=<ELEVENLABS_API_KEY>
# One Deepgram key serves BOTH capabilities — Flux STT and Aura TTS.
DEEPGRAM_API_KEY=<DEEPGRAM_API_KEY>
```

Rules:
- Database rows store only `env:VAR` secret *references* (`provider_defs.secret_ref`).
- Keys never appear in API responses, logs, audit entries or the Redis config cache.
- A missing key is a **warning** at save time and a sanitized hard error when a
  session starts or a connection test runs. Startup only fails on missing keys
  in production when the affected provider is the platform default.
- Any key that was ever committed or pasted somewhere must be treated as
  compromised and rotated at the provider before being set here.

Optional endpoint overrides: `SARVAM_TTS_WS_URL`, `ELEVENLABS_WS_BASE`,
`DEEPGRAM_API_BASE`, `DEEPGRAM_WS_BASE` (regional hosts / gateways / mocked
verification).

### Deepgram regions

`DEEPGRAM_REGION` selects the endpoint every Deepgram adapter talks to;
a bot may override it per engine with the Deepgram TTS model's `region`
parameter. All hosts take the same API key and serve the same API
(`shared/providers/deepgram_common.py`):

| `DEEPGRAM_REGION` | REST | WebSocket |
| --- | --- | --- |
| `global` (default) | `https://api.deepgram.com` | `wss://api.deepgram.com` |
| `in` | `https://api.in.deepgram.com` | `wss://api.in.deepgram.com` |
| `eu` | `https://api.eu.deepgram.com` | `wss://api.eu.deepgram.com` |
| `au` | `https://api.au.deepgram.com` | `wss://api.au.deepgram.com` |

A region is **data residency only**. The India endpoint runs the same models
as the global one and adds no languages — see the Deepgram TTS note below.

## Catalog

- `provider_defs` — provider registry (kind, code, display name, secret ref, status).
- `provider_models` — per model: capability (stt/tts/llm), provider-native
  language codes, codecs, sample rates, streaming flag and `params_schema`
  (drives both the dynamic UI and backend range validation).
- `voice_profiles` — voices/speakers: `provider_voice_id` is the exact wire
  code (lowercase Sarvam speakers, ElevenLabs voice IDs), plus supported
  locales, model codes and per-voice default settings.

Seeded by `python -m backend.cli seed`. Operator metadata is generally
preserved, while the governance reconciliation deliberately converges
provider/model status to the allowed live matrix. Sarvam bulbul:v3 ships
with 37 speakers (default `shubh`) and 11 languages; ElevenLabs with
`eleven_flash_v2_5` and 8 voices; OpenAI with the GPT-4o family.
Odia is `or-IN` platform-side and translated to Sarvam's `od-IN` on the wire
(`shared/providers/languages.py`).

### Deepgram TTS (Aura / Aura-2) — language scope

Deepgram's model code in the catalog is the Aura **generation** (`aura-2`,
`aura`); the individual voice (`aura-2-thalia-en`) is the wire `model` query
parameter and lives on the voice row. There is no language parameter at all:
the language is the voice id's suffix, so the platform locale is never sent
to Deepgram in any form.

Verified against developers.deepgram.com/docs/tts-models on 2026-09-21,
Deepgram text-to-speech speaks **English, Spanish, German, Dutch, French,
Italian and Japanese** — Aura v1 is English-only. It has **no Hindi, Tamil,
Telugu, Malayalam, Marathi, Gujarati, Punjabi or Urdu voice**, and the India
endpoint does not add one.

Of the platform's nine enabled languages only `en-IN` maps, to Deepgram's
English voices — which carry American, British, Australian, Irish and
Filipino accents. **There is no Indian-English Aura voice**, so a bot that
needs one belongs on Sarvam or ElevenLabs.

Because Deepgram has no language field to reject a mismatch, selecting an
unsupported language would produce an English voice reading foreign text
rather than an API error. Both adapters therefore refuse it themselves,
against the explicit tables in `shared/providers/languages.py`, and so does
the preview endpoint.

Current governed live matrix:

| Capability | Active production providers | Platform default |
| --- | --- | --- |
| STT | `sarvam` (`saaras:v3` default, `saaras:v4` selectable, `saarika:v2.5`), `deepgram` | `sarvam/saaras:v3` |
| TTS | `sarvam`, `elevenlabs`, `deepgram` | `sarvam/bulbul:v3`, voice `shubh` |
| LLM | `openai` | `openai/gpt-4o-mini` |
| Embedding | `openai` | `openai/text-embedding-3-small` |

The code registry contains additional dormant adapters, but a live bot cannot
select them unless governance/catalog status is changed in code. `mock` remains
a development/test pseudo-provider and is excluded in production.

### Sarvam saaras:v4 and key terms

`saaras:v4` (catalog row from migration `a9c1e3b5d7f9`, or pre-staged with
`backend/scripts/stage_saaras_v4_catalog.py`) is selectable per bot; `saaras:v3`
keeps `is_default` and remains the platform default, so nothing changes for a
bot that never picked a model. v4 uses the same WebSocket endpoint, language
codes, modes and VAD controls as v3 and adds **key-term biasing**:

- `voice_bot_settings.stt_settings.keyterms` — a list of up to 50 terms
  (≤ 64 characters each, one term or phrase per entry). The key exists only in
  the v4 `params_schema`, so the Voice tab renders it only for v4 and the API
  rejects it for any other model (`unknown parameter 'keyterms'`). The PUT
  trims, de-duplicates and drops an empty list; `shared/providers/stt/
  sarvam_keyterms.py` holds the limits (Sarvam does NOT reject over-limit or
  comma-joined lists at handshake time — verified 2026-10-01).
- Runtime: `build_stt_service` hands the cleaned list to
  `EndpointedSarvamSTTService`, whose connect proxy adds
  `keyterms=<JSON array>` to the Sarvam handshake — only for v4; a stale v3
  configuration logs a warning and records `stt_keyterms sent=false`. The
  identifier-recovery REST transcriber forwards the same list (v4 only).
- pipecat ≥ 1.11 ships `saaras:v4` in its own Sarvam model table (the project
  pins `pipecat-ai==1.12.0`; `voice_runtime/sarvam_stt.py` refuses to import
  on an older pipecat). A deployment still running pipecat ≤ 1.6 cannot run
  v4 and must keep the catalog row `inactive` (the staging script's
  `--status inactive`; the migration activates it on upgrade).
- pipecat 1.12 notes for this stack: Silero VAD is a core dependency (no
  `[silero]` extra); the `[sarvam]` extra is not used because it pins
  `sarvamai==0.1.28` (no streaming `keyterms`); Deepgram Flux now *proposes*
  turn boundaries (`ProposedUserStoppedSpeakingFrame`) that the pipeline's
  `ExternalUserTurnStrategies` resolve into the `UserStoppedSpeakingFrame`
  the brain gates on. Output-transport contract: `_write_frame`
  / `write_audio_frame` MUST return whether the frame was taken — a falsy
  result now skips the realtime pacing sleep and the downstream push
  (recorder, echo reference), so a bare `return` in an override turns paced
  playback into a burst. The media sender's partial PCM lives in
  `_audio_runs` + the resampler tail (`_clear_audio_buffer()`), and a reply's
  last partial chunk is flushed padded with silence instead of dropped.

### Sarvam streaming socket: the first turn, and audio before the socket is live

- **First-turn speech requirement.** Sarvam's streaming server opens the FIRST
  speech segment of a connection only after `first_turn_min_speech_frames`
  consecutive speech frames (server default 8; one frame = 512 samples =
  64 ms at 8 kHz, i.e. ~0.5 s of speech above `positive_speech_threshold`),
  while every later segment needs `min_speech_frames` (default 2). A short
  first answer after the greeting ("haan boliye", ~0.5 s) never reached that
  bar: the flush at VAD stop had nothing to finalize, no `data` message came
  back, the adapter logged `stt_missing_final_retry` + `stt_final_missing`,
  the turn ended empty and the silence prompt spoke 15 s later (live
  2026-10-22, cv_80982e36f4f5 / cv_baeaf87927cb; reproduced offline by
  replaying the recorded audio — the same audio transcribes once the parameter
  is lowered). `build_stt_service` therefore sends
  `first_turn_min_speech_frames` = the bot's `min_speech_frames` (or Sarvam's
  default 2) unless the bot's STT settings set it explicitly.
- **Audio before the socket is live.** Both Sarvam adapters hold inbound audio
  that arrives while no socket is live — the handshake at call start, the
  legacy adapter's dead-socket window before its bounded reconnect, and (on
  the realtime socket under manual endpointing) the gap until the always-open
  utterance has been announced with `speech_start` — and send it as soon as
  the socket is ready, oldest first, bounded to the newest 6 s. The call
  records `stt_audio_buffered_before_connect` with `buffered_ms` /
  `dropped_ms` (legacy: plus `reconnect`) whenever that happened. Invariant:
  no caller audio is dropped because a downstream service was not ready.

### Sarvam realtime STT (`SARVAM_REALTIME_STT`, evaluation flag, OFF)

Partials from this socket reach the barge-in word gate; since 2026-10-22 an
interim only confirms an interruption with sustained VAD speech and stable
words (see `barge_in_min_words` in VOICE_RUNTIME.md), because the always-open
utterance transcribes the bot's own echo on a phone leg.

Pipecat 1.12 ships `SarvamRealtimeSTTService` for Sarvam's newer
`/speech-to-text-realtime/ws` socket. `voice_runtime/sarvam_realtime_stt.py`
wraps it; `SARVAM_REALTIME_STT=true` on the voice worker routes Sarvam STT
over it instead of the legacy `/speech-to-text/ws` adapter
(`EndpointedSarvamSTTService`). Settings (all `shared.config.Settings`):

| Env var | Default | Values |
|---|---|---|
| `SARVAM_REALTIME_STT` | `false` | `true` routes `saaras:v3` (as `saaras:v3-realtime`) and `saaras:v4`; any other model keeps the legacy socket |
| `SARVAM_REALTIME_ENDPOINTING` | `manual` | `manual`: our VAD + turn policy send `speech_start`/`speech_end`; `vad`: Sarvam's server VAD proposes the boundaries (ExternalUserTurnStrategies, like Flux) |
| `SARVAM_REALTIME_STREAM_TYPE` | `balanced` | `fast` gives the first partial ~0.6 s into speech instead of ~1.1 s; finals are equally fast |

What the endpoint does differently (probed directly 2026-10-06, 16 kHz,
real-time paced audio, `scratchpad/realtime_probe.py`):

- `keyterms` is a JSON array query parameter, `saaras:v4` only, and the
  realtime socket ENFORCES the contract at the handshake (close code 4000
  for keyterms on `saaras:v3-realtime`, for 51 terms, for a 65-character
  term); the legacy socket silently accepted all three. Our shared rule set
  already enforces the same limits, so nothing changes for operators.
- True `transcript.partial` events during speech (the legacy socket only
  emits segment finals). Partials are decoded BEFORE language/script
  normalisation: Roman-script or English running text for a Devanagari
  final. The adapter therefore tags them display-only (client UI + barge-in
  word gate); the brain's interim-agreement stability score ignores them.
  Without that tag every barge-in segment was rejected as
  `weak_signal:bot_audio_overlap+unstable_transcript`.
- `endpointing=manual` only hears audio from `speech_start` on (a few
  hundred ms of server pre-roll at most). Pipecat sends `speech_start` on
  `VADUserStartedSpeakingFrame`, which lands late enough during a barge-in
  to lose the first word(s). The adapter keeps an utterance open at all
  times (`speech_start` after `session.begin` and again right after every
  `speech_end`); duplicates are harmless, 8 s of open silence is fine, and
  back-to-back utterances each get their own final.

Guards added 2026-10-23 after live call cv_a3f4eee0d0d2 (bot with hi-IN +
en-IN, auto-detect; its recorded audio was replayed into the socket to
confirm each cause — `scratchpad/probe/replay_probe_rt.py`):

- **Language.** With `language_code=auto` the realtime socket renders Hindi
  speech as English ("haan boliye" → "Yes, speak." live, "Hello, speak." on
  replay; the legacy socket's `unknown` returned Devanagari for the same
  audio). `build_stt_service` therefore pins a bot with ONE configured
  language to it on this socket whatever `auto_detect_language` says
  (`stt_realtime.language_pinned = single_language_bot`), and always sends
  `mode=transcribe` (`mode_forced` when the bot's STT mode differs). A
  multi-language bot keeps `auto` and inherits the English rendering of
  short Hindi answers — it is NOT ready for this socket until Sarvam's auto
  mode transcribes rather than translates, or until it is pinned to its
  default language.
- **Silence hallucination.** The caller-audio gate passes exact zeros while
  shut. On the open utterance the server decoded 18 s of zeros into
  partials ("However, whatever happens, happens…") from 4 s on and a
  server-initiated final at ~18 s in a random language (Tamil live, Kannada
  on replay). Live that 2-word final confirmed a barge-in and cut a 19 s
  reply at 16 s; the partials landed in the caller's next turn. The adapter
  never sends exact-zero frames; while nothing is sent it pings every 8 s
  (`stt_realtime_session_end.silence_skipped_ms`, `pings`).
- **Echo window.** While the bot speaks and for 0.6 s after it stops, audio
  goes to the socket only while Silero reports caller speech; the last
  0.4 s of guarded audio is sent first so the gate's pre-roll (first
  syllable) survives (`guarded_skipped_ms`).
- **VAD evidence.** A partial or final leaves the adapter only while the
  caller is speaking or while a `speech_end` we sent still awaits its final
  (2.5 s); everything else is dropped before the barge-in word gate and the
  brain (`stt_realtime_transcript_dropped reason=no_vad_speech`, recorded
  for finals; partials are counted in `partials_dropped`). A final that
  lands while the caller is still speaking is tagged mid-utterance exactly
  like the legacy adapter's barge-in flush finals.
- **Echo text.** In the echo window a transcript of ≥2 words that starts
  with a word the bot recently spoke and whose words are ≥60 % recently
  spoken words is the bot's own voice (`reason=echo_text`). The reference
  (`voice_runtime/spoken_text.py`, fed by the streaming TTS router with
  every text it speaks; the segmented REST TTS path does not feed it)
  compares Devanagari and Roman spellings through a shared key. A caller
  who answers over the bot with the bot's own words ("theek hai") is
  dropped in that window too; "haan, bataya gaya tha" is not (it starts
  with the caller's word).

Remaining, by design: the legacy socket and barge-in word gate are untouched;
carrier-side echo loud enough to open the gate AND trip Silero AND be
transcribed as something other than the bot's words still reaches the brain
(only AEC removes that); silence hallucination is prevented, not detected —
a server that invents text from real low-level noise is still caught by the
VAD-evidence rule only while no `speech_end` is pending.
- `endpointing=vad`: `vad.speech_end` arrives ~1.0 s after the audio ends
  (500 ms server silence window + processing) and the final ~1.3 s after;
  the mode bypasses our pause policy, the word-confirmed barge-in gate and
  the brain's user-speech timing (no local VAD), so `turn_latency` has no
  `stt_final`/`endpoint` fields. Functional smoke only; not recommended.
- Final after `speech_end`: 180–290 ms (n=10). Legacy socket flush → final:
  350 ms (v4, n=1; two of four probes finalised early on the server's own
  VAD, which is the same mechanism that splits sentences in calls).

Call-level comparison, local browser transport, Sarvam v4 + 3 keyterms,
same 2.5 s Hindi caller clip spoken over the greeting (barge-in), pipecat
1.12, `scratchpad/e2e_v4_session.py`, 2026-10-06 (ms, per user turn):

| Path | stt_final | endpoint | llm_ttft | tts_ttfb | response | transcript |
|---|---|---|---|---|---|---|
| legacy streaming #1 | 55 | 407 | 1293 | 238 | 3669* | "…अभी तक। तक नहीं…" (word doubled) |
| legacy streaming #2 | 73 | 424 | 924 | 241 | 1854 | exact |
| legacy streaming #3 | 378 | 730 | 1198 | 249 | 2485 | "…ऑर्डर। अभी तक…" (split in two) |
| realtime manual/balanced #1 | 235 | 588 | 1089 | 251 | 3040* | exact |
| realtime manual/balanced #2 | 235 | 589 | 1425 | 245 | 2966 | exact |
| realtime manual/balanced #3 | 299 | 650 | 1032 | 239 | 2161 | exact |
| realtime manual/fast #1 | 288 | 640 | 1218 | 332 | 3244* | exact |
| realtime manual/fast #2 | 249 | 600 | 1178 | 281 | 2352 | exact |
| realtime manual/fast #3 | 213 | 564 | 849 | 231 | 1816 | exact |

`*` = an orchestration `classify` outlier (0.8–1.7 s) inflated the response.
`stt_final` = VAD stop → final; `endpoint` = VAD stop → turn closed. The
legacy socket's 55–73 ms is its server VAD finalising on the clip's natural
pause BEFORE our flush, the same behaviour that produced the doubled/split
transcripts; realtime finals cost ~+170 ms on the endpoint here and were
exact 6/6. Barge-in during the greeting was confirmed by the partials'
word count in 0.13–0.16 s on the realtime path versus ≥1.0 s of sustained
VAD on the legacy path. LLM TTFT (0.85–1.4 s) dominates both.

Recommendation (2026-10-06): keep the flag OFF on live. Evidence is n=3
per arm, one clip, browser transport. Candidate to become the default
after a telephony (8 kHz) run and a multi-turn soak, with
`SARVAM_REALTIME_STREAM_TYPE=fast`; leave `vad` endpointing alone.

### Dependency pins (2026-10-01)

`requirements.txt` is fully `==`-pinned to the set that passed the suites.
Deliberately held back, with the cap that holds them: `protobuf` 6.x
(pipecat `<7`), `multidict` 6.x (aiohttp `<7`), `uuid-utils` 0.x
(langchain-core / langsmith `<1`), `websockets` 16.1 (langgraph-sdk `<17`),
`onnxruntime` 1.24.x, `soxr` 1.0.x, `soundfile` 0.13.x and `sentencex`
1.0.31 (all pinned by pipecat 1.12.0). `mcp` is on 2.x: `FastMCP` became
`mcp.server.mcpserver.MCPServer` and `stateless_http` moved to
`streamable_http_app(...)` (`backend/mcp_server/server.py`). Frontend: pnpm
(`pnpm-lock.yaml` is authoritative; `package-lock.json` is a stale npm
artifact, now deleted), Vite 8 (Rolldown), Vitest 5, React 19, React Router 7,
TypeScript 7 (`baseUrl` removed; `paths` are relative). Node ≥ 22.12 is
required by Vite 8 / Vitest 5 and jsdom 30 wants 22.22+; install the
frontend with `pnpm install --frozen-lockfile` (pnpm 10.32, declared in
`package.json` `packageManager`; `corepack prepare pnpm@10.32.0 --activate`
or `npx pnpm@10.32.0`), never with `npm install`.

### openai 3 (SDK) and TLS

The project pins `openai==3.22.1`. The only breaking change in the 3.x line
is the HTTP stack: the SDK uses `httpx2` and no longer installs `httpx`
itself (our own adapters still use `httpx`, which stays in requirements).
`httpx2` verifies TLS against the operating-system trust store through
`truststore`, not `certifi`; `SSL_CERT_FILE` / `SSL_CERT_DIR`, when set,
override it. Both local and live validate `api.openai.com` with the stock
Ubuntu bundle (`/etc/ssl/certs/ca-certificates.crt`) and neither host has a
proxy configured for the EchoSphere units, so no certificate variables are
needed. Our adapters pass plain-float timeouts and never a custom
`http_client`, so no call-site change was required; all four adapters (chat
completions incl. streaming and tool calls, embeddings, Whisper, TTS) were
exercised against the live API on 3.22.1. The dormant Anthropic adapter is
not installed (`anthropic` is governance-inactive); if it is ever enabled on
anthropic ≥ 1.0, `temperature`/`top_k`/`top_p` must move to `extra_body`.

## Runtime data flow

```
caller/browser → Sarvam STT WS (saarika/saaras v3|v4, auto-detect supported;
                                 saaras:v4 adds ?keyterms=[…] key-term biasing)
  → transcript → VAD/turn control → intent routing → KB retrieval (when needed)
  → OpenAI LLM token stream → sanitizer → sentence buffer
  → StreamingTTSRouter → Sarvam TTS WS | ElevenLabs TTS WS
  → paced audio → browser (PCM 24 kHz) or telephony (8 kHz via serializers)
```

- One persistent TTS WebSocket per call and provider; sentences never open
  connections. ElevenLabs voice changes reconnect (voice is in the URL);
  Sarvam voice/language changes re-send config on the same connection.
- Turn taking: Sarvam finalizes a transcript every time the local VAD flushes
  (~0.2 s pause), so STT finals are per SEGMENT. The brain buffers segments
  and answers only when the turn controller closes the user's turn
  (VAD `stop_secs` + `user_speech_timeout` of silence ≈ 1 s by default) — a
  caller pausing mid-sentence is never talked over. Transport-aware defaults
  (telephony uses lower VAD volume/confidence thresholds for quiet 8 kHz PSTN
  audio) can be overridden per bot via `voice_bot_settings.stt_settings.
  turn_detection` `{confidence, start_secs, stop_secs, min_volume,
  barge_in_min_words, user_speech_timeout}` — see `voice_runtime/pipeline.py
  resolve_turn_detection` for the clamped ranges.
- Barge-in: while the bot is quiet, VAD starts the user's turn (fast path);
  while it is SPEAKING, an interruption must be confirmed by a transcript of
  ≥ `barge_in_min_words` words (default 2, 0 = interrupt on any voice
  activity) — otherwise background speech reaching the mic cancels replies
  mid-word (`voice_runtime/barge_in.py`). Sub-threshold segments that arrive
  during bot audio (backchannels like "हाँ", noise fragments) are held by the
  brain and answered once the reply finishes playing, never by cutting it.
- Hang-up: `shared/orchestration/router.py detect_hangup()` matches Hindi /
  Hinglish / English disconnect requests (negation-guarded) on every STT
  segment, before workflows and the LLM. The brain interrupts playback,
  speaks one short acknowledgement in the caller's language
  (`shared/orchestration/phrases.py`), ends the worker (telephony serializers
  emit the protocol `stop`) and drops any later STT events.
- Language following: the reply language tracks the caller per utterance
  (script-checked, incl. romanized Hinglish → Hindi); canned fallbacks and
  workflow-engine retry/handover strings are localized via
  `shared/orchestration/phrases.py`. Only languages the bot is configured
  for (`bot_languages`) are eligible — enable the locale to allow switching.
- Barge-in: Pipecat interruption cancels the LLM task, closes the ElevenLabs
  context / drops the Sarvam connection, clears queued audio, and rejects any
  late chunks (generation IDs on both provider and router side).
- Per-language voices: `voice_bot_settings.language_voice_map` maps locales to
  `{provider, model, voice}`; the detected transcript language switches the
  active engine for the next reply without unnecessary reconnects.
- Fallback: `fallback_provider/model/voice` engage only for transient failures
  (timeout, rate limit, upstream errors, connection loss) — never for auth or
  configuration errors. Which engine actually spoke is recorded per reply in
  the `tts_provider_used` voice event (`tts_fallback` marks switches).

## APIs (all under `/api/v1`, authenticated)

| Endpoint | Purpose |
|---|---|
| `GET /providers/catalog?capability=` | active providers + hasCredentials flag |
| `GET /providers/{cap}/{code}/models` | models incl. `paramsSchema` |
| `GET /providers/{cap}/{code}/models/{model}/languages` | platform locales the model supports |
| `GET /providers/tts/{code}/voices?model=&language=&gender=` | voices/speakers |
| `POST /providers/validate-config` | full config validation (errors + warnings) |
| `POST /providers/test` | REAL connectivity test (requires `manage_voices`) |
| `POST /providers/tts-preview` | server-proxied voice preview with TTFA/total timing |
| `POST /providers/elevenlabs/sync-voices` | verify catalog voices against the account |
| `PUT /bots/{id}/voice-settings` | save; rejects invalid combos with error list |

The same validation used by `validate-config` gates every save — frontend
field-hiding is never the only check. Provider tests and previews are
audit-logged (never the preview text itself, only its length).
