# Voice Provider Configuration (Sarvam AI · OpenAI · ElevenLabs)

The realtime voice stack is fully database-driven: which providers, models,
languages, voices and parameters a Voice Bot may use comes from the provider
catalog tables, never from hardcoded lists.

## Credentials

Set in the root `.env` (shared by the API and the voice runtime — the file is
loaded into the process environment at startup):

```env
OPENAI_API_KEY=
SARVAM_API_KEY=
ELEVENLABS_API_KEY=
```

Rules:
- Database rows store only `env:VAR` secret *references* (`provider_defs.secret_ref`).
- Keys never appear in API responses, logs, audit entries or the Redis config cache.
- A missing key is a **warning** at save time and a sanitized hard error when a
  session starts or a connection test runs. Startup only fails on missing keys
  in production when the affected provider is the platform default.
- Any key that was ever committed or pasted somewhere must be treated as
  compromised and rotated at the provider before being set here.

Optional endpoint overrides: `SARVAM_TTS_WS_URL`, `ELEVENLABS_WS_BASE`
(regional hosts / gateways / mocked verification).

## Catalog

- `provider_defs` — provider registry (kind, code, display name, secret ref, status).
- `provider_models` — per model: capability (stt/tts/llm), provider-native
  language codes, codecs, sample rates, streaming flag and `params_schema`
  (drives both the dynamic UI and backend range validation).
- `voice_profiles` — voices/speakers: `provider_voice_id` is the exact wire
  code (lowercase Sarvam speakers, ElevenLabs voice IDs), plus supported
  locales, model codes and per-voice default settings.

Seeded by `python -m backend.cli seed` (initial data only — rows are editable
via master data and never overwritten by re-seeding). Sarvam bulbul:v3 ships
with 37 speakers (default `shubh`) and 11 languages; ElevenLabs with
`eleven_flash_v2_5` and 8 voices; OpenAI with the GPT-4o family.
Odia is `or-IN` platform-side and translated to Sarvam's `od-IN` on the wire
(`shared/providers/languages.py`).

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
