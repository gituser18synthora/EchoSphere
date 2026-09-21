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
| STT | `sarvam`, `deepgram` | `sarvam/saaras:v3` |
| TTS | `sarvam`, `elevenlabs`, `deepgram` | `sarvam/bulbul:v3`, voice `shubh` |
| LLM | `openai` | `openai/gpt-4o-mini` |
| Embedding | `openai` | `openai/text-embedding-3-small` |

The code registry contains additional dormant adapters, but a live bot cannot
select them unless governance/catalog status is changed in code. `mock` remains
a development/test pseudo-provider and is excluded in production.

## Runtime data flow

```
caller/browser → Sarvam STT WS (saarika/saaras, auto-detect supported)
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
