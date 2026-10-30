# STT language detection and mid-call language switching

A call can only follow the caller between languages if the speech recognizer
is allowed to *detect* the spoken language. When the recognizer is pinned to
one language it labels every utterance with that language (and transliterates
foreign speech into that script), so the brain's language-switch logic never
sees a change.

## The rule (`shared/providers/stt_language_policy.py`)

Precedence, highest first:

1. **Explicit STT language** (`voice_bot_settings.stt_language`, e.g. `hi-IN`)
   → recognizer pinned to it. `unknown` counts as blank.
2. **Explicit `stt_settings.auto_detect_language`** (`true` / `false`) → the
   user's choice wins.
3. **Derived default** from the bot's *effective languages*:
   more than one language → auto-detect **on**; one language → **off**
   (pinned to the bot's default language, the reliable choice for short,
   narrowband phone replies).

*Effective languages* = the bot's own `bot_languages` when it has any,
otherwise the tenant's `tenant_settings.default_languages`.

The persisted key is deliberately **tri-state** (absent / true / false). The
Voice tab writes it only when the user touches the control, so a fresh
multilingual bot keeps following the derived default and an explicit OFF
survives later language changes. "Use automatic" removes the key again.

## Where it applies

- `shared/bot_config.resolve_bot_config` computes the decision once, stamps the
  effective boolean into `stt.settings.auto_detect_language` and keeps the
  provenance in `stt.auto_detect_language` (`source: explicit | derived`).
  It also resolves `languages` and the default call language from the
  effective list (tenant inheritance).
- `voice_runtime/pipeline.build_stt_service` (Sarvam realtime STT) pins or
  auto-detects from that decision **on every transport** — browser Testing
  and telephony behave the same. The former telephony-only pin
  (`prefer_primary_language`) is gone; the parameter is accepted and ignored.
  Each call records a `stt_language_mode` event:
  `{mode: auto|pinned, language, auto_detect_language, source, configured_languages, default_language}`.
- `GET/PUT /bots/{id}/voice-settings` returns `sttAutoDetectLanguage`
  `{value, effective, source, derivedDefault, languages}` beside the raw
  `sttSettings`.
- Voice tab → Speech-to-Text card → **Auto-detect language** toggle (right
  under the STT language selector, not under Advanced) with the reason
  ("Automatic default: on (2 languages configured)" / "Set manually to off")
  and a "Use automatic" reset. `src/components/SttAutoDetectControl.tsx`.
- The generic provider-parameter renderer (`ProviderParams.tsx`) never
  pre-fills or renders `auto_detect_language`; the catalog schema entry has
  no default and `widget: "auto_detect_language"`.

## What switching still guards against (unchanged, `voice_runtime/brain.py`)

- only languages in the bot's configured set can become the call language
  (`_match_supported`); anything else raises `language_unsupported` and the
  transcript gate rejects the segment (`stt_segment_rejected unsupported_script`);
- short answers (`haan`, `yes`, `okay`, numbers, technical payload) never
  switch (`language_switch_blocked numeric_or_technical_payload` /
  `short_answer_in_workflow`);
- romanized Hinglish with a Hindi lexical leaning never flips to English.

Useful events per call (Mongo `conversation_transcripts.events`):
`stt_language_mode`, `language_detected`, `language_switch_blocked`,
`language_unsupported`, `tts_language_switched`, and
`orchestration_turn.user_language / response_language`.

## Legacy rows

Before this rule the Voice tab pre-filled every Sarvam schema default on
save, so many multilingual bots carry `auto_detect_language: false` that
nobody chose. Those are treated as explicit (respected). Fix them either from
the Voice tab ("Use automatic" or toggle ON) or in bulk with

```
env/bin/python backend/scripts/backfill_auto_detect_language.py            # dry run
env/bin/python backend/scripts/backfill_auto_detect_language.py --apply    # writes + cache invalidation
```

which clears `false` only for bots whose effective languages number more than one.

## Precedence table (verified 2026-09-16, incl. Malayalam)

| Configuration | Recognizer | Why |
|---|---|---|
| `stt_language` = `ml-IN` (explicit fixed STT language) | pinned `ml-IN` | a genuinely selected fixed language is the only thing that outranks detection |
| `stt_language` blank, `auto_detect_language` absent, bot en/hi/ml | auto | derived: more than one effective language |
| `stt_language` blank, bot default (`language_voice_map.default`) = `ml-IN`, no pin | auto | a default/primary language is never an STT pin |
| `auto_detect_language: true` + `stt_language` = `ml-IN` | pinned `ml-IN` | explicit fixed language wins over explicit detection |
| `auto_detect_language: false`, default `ml-IN` | pinned `ml-IN` | explicit user choice |
| bot with only `ml-IN` | pinned `ml-IN` | derived: one language |

`stt_language` is never auto-populated: the Voice tab writes it only from the
STT Language selector (blank = "Auto-detect" option), the API stores what it
receives, and `unknown` counts as blank. The runtime records the outcome on
every call as `stt_language_mode`.

## Tenant / bot language inheritance

`tenant_settings.default_languages` is the tenant's language *entitlement*
(Super Admin, `shared/tenant_languages.py`): the languages catalog offered to
tenant users (`GET /languages?tenantId=…`) and therefore the Overview tab's
"Edit languages" choices are restricted to it. A bot's `bot_languages` is the
bot's own selection from that entitlement; languages already on a bot are
retained even if later removed from the tenant. The bot's list is thus the
supported set; the tenant list only fills in when a bot has none.

| Tenant | Bot | Effective languages | Auto-detect default |
|---|---|---|---|
| en, hi, ml | none | en, hi, ml (inherited) | on |
| en, hi, ml | hi only (primary stored) | hi | off (pinned hi) |
| en, hi, ml | ml only (explicitly restricted) | ml | off (pinned ml) |
| hi | hi, ml (retained/explicit) | hi, ml | on |
| en, hi | ta, ml | ta, ml (bot wins) | on |
| hi, ml | none | hi, ml (Malayalam inherited) | on |
| any | en, ml | en, ml | on |

Only languages in the effective set can become the call language
(`brain._match_supported`); the transcript gate's allow-list is the platform
default ∪ the effective set.

## Malayalam findings (cv_eb5cdace6a98, cv_78c1236a5cdf — 2026-09-16)

Both were browser calls on the mPokket Bot-Dev (en/hi/ml/ta, default ml-IN,
`auto_detect_language: false` legacy → STT pinned `ml-IN`). Sarvam saaras:v3
transcribed the Malayalam correctly ("ഹാ പറഞ്ഞോളൂ ഗോരവമാണ്" = "yes, go
ahead, it's Gaurav"; REST re-transcription of the recording agrees). The
failure was AFTER STT:

1. The collections identity gate (regex fallback — the bot has
   `goal_engine_enabled: false`) knew no colloquial Malayalam yes ("ഹാ", "ആ",
   "ഉം") and no "<name> ആണ്" claim → `identity_unclear` → scripted re-ask.
2. `CollectionCallPolicy.language` was overridden by the customer record's
   `preferred_language` (hi-IN) → the re-ask was spoken in **Hindi** on a
   Malayalam call. The policy language now follows the call language passed
   by the brain/simulate; the stored preference only fills an unspecified
   language and is recorded as `context_preferred_language_noted`.
3. In cv_78c1236a5cdf the second utterance (2 s, quiet, during bot audio) was
   genuinely unintelligible — REST gives "ഗർബനാ" / Bengali "গর্ব বানা" — an
   audio/STT-quality problem, not a language-switch one.

Provider notes for Malayalam (Sarvam):
- STT REST/streaming accept `ml-IN`; auto-detect labels Malayalam as `ml-IN`
  with p≈0.9 on clear 16 kHz audio, but very short sounds are often mislabelled
  ("ശരി" → Hindi "शरी।", "ഉം" → English "Um"); the short-segment re-read
  usually fails (`short_segment_retranscribe_failed reason=script`) and the
  switch guard blocks them (`too_few_words`), so the call language holds. On
  8 kHz telephony a 2 s Malayalam opener was once heard as English "Half an
  hour" — auto-detect on narrowband audio is less reliable than on browser
  audio; the identity gate recovered on the next segment.
- TTS bulbul:v3 speaks Malayalam (voice priya). gpt-5-mini replies in
  Malayalam sometimes leak letters from unrelated scripts (Cyrillic, Armenian,
  CJK, Arabic, Telugu); `strip_foreign_scripts` now removes them before
  synthesis (`tts_foreign_script_stripped` event) — the transcript keeps the
  model text.

Switch-guard reasons: `too_few_words` (one meaningful word — "yes", "haan",
"അതെ"), `numeric_or_technical_payload` (digit words / business terms were
stripped), `short_answer_in_workflow`, lexical `leaning` mismatch.

## Local test tooling

- `backend/scripts/telephony_latency_probe.py`: `PROBE_QUIET_GAP=3.0` for
  bots with long replies; turns may carry a `ml-IN::` / `en-IN::` prefix
  (caller TTS language), `VAANI_SIM_FROM` picks the caller number (a number
  with previous-call memory gets the memory-continued greeting).
