# Migration from the legacy `VoiceBot/` folder

The `VoiceBot/` directory (added in commit `70c41bf` "Added Voice-Bot Files") was a
FreeSWITCH-oriented STT/LLM/TTS engine with its own MongoDB config store. On this
branch it has been **removed from the working tree**; everything usable was ported
into `backend/`, everything broken or superseded was dropped. Nothing is lost:
**git history preserves the entire folder** —

```bash
git show 70c41bf --stat          # the commit that added it
git show 'HEAD^{/Added Voice-Bot Files}':VoiceBot/adapters/base.py   # read any old file
```

`VoiceBot/.env` values were preserved **commented-out in the root `.env`** (section
"Preserved from VoiceBot/.env before folder removal") — uncomment what you need;
provider adapters read the same `*_API_KEY` env vars.

## Why the folder could not run

- **Import case mismatch**: the code imported the package as lowercase `voicebot`
  while the directory was `VoiceBot/` — unimportable on case-sensitive Linux
  filesystems.
- **Two incompatible import roots**: modules assumed different top-level packages,
  so no single `PYTHONPATH` satisfied the whole tree. The legacy test suite failed
  collection with 11 import errors before any test ran.

## What was migrated (with bug fixes)

### STT/TTS/LLM adapters → `shared/providers/`

Each adapter was rewritten against the typed provider interface
(`shared/providers/base.py`) and registered in `shared/providers/factory.py`.
Bugs fixed during the port:

| Provider | Legacy defect | Fix in `shared/providers/` |
|---|---|---|
| Deepgram STT | invalid SDK call (method didn't exist in the pinned SDK) | direct httpx REST implementation (`stt/deepgram.py`) |
| Google TTS | language hardcoded to `en-US` regardless of bot config | uses the configured language (`tts/google_tts.py`) |
| Anthropic LLM | no tools, no system prompt, no streaming | tools + system + token streaming implemented (`llm/anthropic_llm.py`) |
| Azure TTS | shared mutable speech config raced across concurrent calls | per-call configuration (`tts/azure_tts.py`) |
| AssemblyAI STT | API key set on a global module singleton | per-instance key (`stt/assemblyai.py`) |
| Sarvam | Odia language code `od-IN` (invalid) | corrected to `or-IN` |
| Audio utils | three duplicate pure-Python resamplers | one numpy implementation, `shared/audio/pcm.py` |

### Text processing

`tts_text.py` + `sentence_splitter.py` → `shared/audio/text.py`
(pure functions, stdlib only, Indic-script aware: Devanagari danda handling,
abbreviation-safe sentence splitting, lead-in merging).

### Routing design

The `orchestrator` / `rag_router` / `intent_engine` trio was not ported line-by-line;
its **design** (domain-word gating, smalltalk skip-lists, intent sample voting,
call-control precedence) was carried into a stateless rewrite:
`shared/orchestration/router.py` (`TurnRouter`) + `voice_runtime/brain.py`
(`ConversationBrain`). See [VOICE_RUNTIME.md](VOICE_RUNTIME.md).

## What was dropped, and why

| Legacy component | Reason |
|---|---|
| `config_layer/` + `api/` tab1–tab7 Mongo config APIs | superseded by the backend's MySQL bot configuration (`voice_bots`, `voice_bot_settings`, prompts, intents) and `resolve_bot_config` |
| sounddevice microphone harnesses | superseded by Pipecat transports (browser WS + telephony serializers) |
| `mcp/mcp_client.py` | duplicate MCP client; the platform now ships an MCP **server** (`backend/mcp_server/`), and the voice runtime calls `KnowledgeService` in-process |
| `adapters/rag/pgvector_rag.py` | orphaned (nothing imported it); superseded by the knowledge plane (`shared/knowledge/`) |
| usage publisher | coupled to a `messaging` package that does not exist in the repo |
| RabbitMQ-era code | broken/unreferenced; background work now uses the Postgres job queue |

## Old → new map

| Legacy (`VoiceBot/`) | Current |
|---|---|
| `adapters/stt/*`, `adapters/tts/*`, `adapters/llm/*` | `shared/providers/{stt,tts,llm}/` |
| `adapters/audio_utils.py`, `audio/pcm_utils.py` | `shared/audio/pcm.py` |
| `audio/tts_text.py`, `audio/sentence_splitter.py` | `shared/audio/text.py` |
| orchestrator / rag_router / intent_engine | `shared/orchestration/router.py` + `voice_runtime/brain.py` |
| FreeSWITCH integration | `voice_runtime/freeswitch.py` (ESL) + `RawPCMSerializer` media path |
| Mongo `voicebot_configs` | MySQL bot config + Redis `botcfg:*` snapshot cache |
| `VoiceBot/tests` (failed collection) | `tests/` — 122 passing tests, see [TESTING.md](TESTING.md) |

## Behavioral differences to be aware of

- Configuration is **published-release driven**: phone/SIP calls require a published
  bot; the legacy engine read live Mongo config.
- Per-bot provider selection now lives in `voice_bot_settings`
  (migration `b2e4f6a8c0d2`); `NULL` columns fall back to env defaults instead of a
  single global provider set.
- Sessions are issued by the API and trusted via Redis — the worker no longer
  derives identity from connection metadata.
- Audio resampling behavior changed from per-sample loops to numpy linear
  interpolation (faster; numerically equivalent for 16-bit PCM purposes).
