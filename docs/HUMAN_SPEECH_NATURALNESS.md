# Human speech naturalness

EchoSphere resolves one sparse configuration in this order: platform defaults,
tenant overrides, then bot overrides. The tenant and Voice Studio forms display
the effective value and its source. Clearing an override restores inheritance;
it does not copy the current effective value into that layer.

## Language fallback

Naturalness pools are selected by the locale's base language only. The enabled
platform locales `en-IN`, `hi-IN`, `gu-IN`, `ml-IN`, `mr-IN`, `pa-IN`, `ta-IN`,
`te-IN`, and `ur-IN` have independent native pools. A locale never borrows a
pool merely because it shares a script with another language. Unknown or
unsupported languages suppress fillers, acknowledgements, corrections and
backchannels; the semantic response still synthesizes normally.

TTS engine selection is separate and follows exact locale mapping, base-language
mapping, then the default engine. Voice identity uses that same resolver.

## Safety and delivery

The conversation runtime sets structured criticality from the route, validated
caller signal, planned policy action and tool status before any preface can be
spoken. Regex detection on each final TTS segment remains a second safety net.
Critical delivery suppresses fillers and self-correction, removes rate jitter,
never increases the configured rate, and uses a bounded clear pause/style plan
without changing semantic text.

Provider/model capability metadata gates native delivery parameters. ElevenLabs
can vary rate between independent WebSocket contexts; Sarvam WebSocket settings
are not changed mid-generation because a config resend force-flushes its socket.
REST sentences are independent and may use rate where the adapter supports it.
Unsupported emphasis, pitch, energy, question or emotional controls degrade to
interruptible sentence/phrase segmentation and planned silence. There is no
large blocking sleep and no complete-response audio buffer.

Streaming self-correction is intentionally disabled. The current token path
would need unsafe look-ahead or risk replaying text already sent to TTS. Rare
self-correction remains available only for an explicitly enabled, non-critical
direct/full-text response.

## Breathing and filler words are independent

Two families cover the gap before a reply, and each has its own master switch
under the layer master `enabled`:

| Family | Master key | Members | Tunables |
|---|---|---|---|
| **Breathing** (nonverbal) | `breathing` | `latency_fillers` (pre-reply breath), `sentence_breaths` (in-reply inhale) | `latency_filler_delay_ms`, `latency_filler_kind`, `filler_audio_selection`, `breath_gain_db`, `sentence_breath_probability` |
| **Filler words** (spoken) | `filler_words` | `acknowledgements` (dispatch-time "जी…"), `latency_filler_ladder` (voiced "Hmm…" + spoken "एक सेकंड…"), `adaptive_latency_cues`, `thinking_fillers` (question beat), tool-lookup prefaces | `acknowledgement_probability`, `latency_cue_probability`, `latency_filler_hmm_ms`, `latency_filler_spoken_ms`, `latency_filler_cue_selection`, `thinking_filler_probability`, `tool_ack_probability` |

Both families play through the same `LatencyFillerProcessor`
(`voice_runtime/latency_filler.py`), but neither switch implies the other:

- `breathing` off (or `latency_fillers` off) → `breath_enabled=False` on the
  processor. The first deadline may still play a ready acknowledgement; the
  voiced cues keep their own schedule (`latency_filler_skipped` reason
  `breathing_off` marks the rung that would have breathed).
- `filler_words` off → no acknowledgement is planned (`plan_early_ack` reason
  `disabled`), no cue library and no ladder rungs are wired, tool prefaces
  are suppressed (`filler_words_disabled`). The breath plays exactly as
  configured.
- The processor exists while any of the three — pre-reply breath,
  acknowledgement, ladder — is on (`SpeechNaturalnessPlanner.latency_cover_enabled`);
  with both families off there is no processor at all.
- `latency_filler_delay_ms` is the deadline of the FIRST gap sound, whichever
  family provides it (the acknowledgement replaces the breath when ready).
- Only an actual breath sound updates the clip library's `note_played`
  clock, so a spoken cue never suppresses the in-reply sentence inhale.

Planner properties: `breathing_enabled`, `latency_fillers_enabled` (breath
only), `sentence_breaths_enabled`, `filler_words_enabled`,
`acknowledgements_enabled`, `latency_filler_ladder_enabled`,
`adaptive_latency_cues_enabled`, `latency_cover_enabled`. Backchannels
(`backchannels`) and the delivery switches are separate and unaffected.

## Latency acknowledgements

The main response starts processing at turn dispatch. A short acknowledgement
is planned separately and becomes eligible only at `latency_filler_delay_ms`
(default 1500 ms from speech end). It uses the existing voiced-cue cache and
latency-filler processor, never the answer's TTS queue. If response audio wins
the race, the acknowledgement is skipped. A missing cached clip is rendered
in the background; the first filler stage uses its configured breath instead
of waiting. A ready acknowledgement replaces that breath and suppresses the
following `hmm` stage. It streams without an audio look-ahead cushion and
stops generating immediately at reply audio, without adding a taper chunk.

The token follows what the caller just did, derived deterministically from
their words with no model call:

| Caller just… | Context | Tokens (hi) |
|---|---|---|
| answered the bot's question (inside a workflow, an agreement, a reply of ≤ 4 words) | `answer` | "जी…", "ठीक है…", "अच्छा…", "अच्छा, ठीक है…" |
| asked a question | `question` | "Hmm…", "जी…", "अच्छा…" — never "ठीक है", which would sound like an answer |
| asked something the knowledge base answers | `lookup` | "एक सेकंड…", "देख रहा/रही हूँ…" |
| explained or stated something the bot has not acted on yet | `information` | "जी…", "अच्छा…", "Hmm…" — never "ठीक है", which would sound like acceptance |
| reported a problem ("नहीं मिला", "कट गया", "galat", "problem", a complaint/hardship/wrong-person signal) | `concern` | "जी…", "Hmm…" only — nothing bright, agreeing or surprised |
| is in a serious state (complaint, refusal, hardship, wrong person, agent request) or dictated amounts/identifiers | `neutral` | "जी…", "Hmm…" only, at half probability — "ठीक है" after a refusal reads as acceptance |

No filler word leads two consecutive turns: the processor reports every
acknowledgement and voiced cue it actually played (`note_early_ack_played`,
`note_latency_cue_played`), and the planner demotes those words for the next
turn's acknowledgement and cue ranking (`plan_latency_cue` reason
`recently_spoken` when every fitting cue was heard on the previous turn;
`plan_early_ack` reason `recently_spoken` when the pool has nothing else).
History follows what was HEARD, not what was planned: a speculative pick
(`plan_early_ack(commit=False)`) sits in `_pending_early_ack` until the
processor confirms it for that turn; a newer plan, a cancellation
(`discard_early_ack`) or teardown (`clear_call_history`) drops it, so a fast
reply or a barge-in never "uses up" a word or the no-consecutive-turns
allowance.

Control: `acknowledgements` on/off; `acknowledgement_probability` (default 0.5,
×1.5 on the first reply after the greeting, the slowest turn of a call); a
hard rule that no two consecutive turns get one (no call opens every reply with
"जी"); skipped acknowledgements do not consume that allowance; pool rotation
with no-repeat; exactly one token, never stacked; nothing
for greetings, hang-up/transfer/safety turns, dictated identifier chunks (the
workflow consumes digits deterministically), unsupported languages, or when
the speculative decision already succeeded (the reply is one routing step
away and a beat would only hold it back). "हाँ…" is deliberately not a token:
after a statement it reads as agreement, not listening. `thinking_fillers`
gates the `question` beat. Languages without dedicated pools reuse their short
acknowledgement/thinking/backchannel pools.

The acknowledgement's audio is bookkept as transient, like a backchannel: the
turn's latency measurement keeps waiting for the reply's first audio, and a
caller who keeps talking over it is finishing a thought (rewind and merge),
not interrupting a reply nobody has heard yet.

Tool lookups keep their own timely preface ("ek minute, main check karta
hoon…"), spoken right before the lookup runs; when a latency acknowledgement
already opened the turn, variants that begin with an acknowledgement word are
skipped so nothing stacks.

## Response start and backchannel evidence

EchoSphere adds no artificial response-start delay: turn detection already
leaves 0.75–1.4 s between the caller's last word and dispatch, which is the
natural human gap, and everything after it is real processing. Simple replies
use normal streaming immediately; eligible generic tool lookups may speak a
short, unambiguous acknowledgement while the lookup runs. Conversational
rhythm comes from real processing, sentence aggregation and planned TTS gaps.

A backchannel requires positive evidence that the caller still owns the floor.
When the audio gate is present, at least 250 ms of current live speech is
required. Otherwise, the open VAD/provider turn (`UserStartedSpeaking` without a
matching stop) is the strongest available live-speech signal. Serious trusted
caller states (complaint, hardship, refusal, wrong person, agent request,
distress or frustration) suppress casual backchannels without an extra model
call. Backchannels never close a caller turn or enter semantic history.

## Sentence breaths and pacing inside a reply

In pause mode (Pause > 0), `sentence_breaths` allows one soft breath before a
long (≥ 10 words) or critical sentence inside a reply — the beat a person takes
before a longer explanation or a verification read-back — at
`sentence_breath_probability` (default 0.35), never before the first sentence
(the pre-reply gap has its own filler) and never more than once per reply. It
is a dedicated gender-matched INHALE clip (~0.3 s, energy rising into the
sentence, brighter and quieter than the pre-reply breath — the exhale-shaped
pre-reply clip, trimmed, read as a cut noise between sentences), inserted after
the planned pause with only a 60 ms beat before the sentence, as TTS audio of
the reply. Operators may supply their own as `inhale_male.wav` /
`inhale_female.wav` in `filler_audio_dir`. Short acknowledgement sentences ("जी।",
"ठीक है।") ride a touch quicker (×1.02–1.05) than questions (×0.95–0.98) and
critical read-backs (×0.96); per-sentence rate applies only where the engine
supports it (ElevenLabs contexts, REST sentences). Digit runs in IDs are
already spaced for digit-by-digit reading by the TTS text preparation.

## Latency fillers

The one silence the layers above cannot cover is the gap between the caller's
last word and the first byte of reply audio: turn detection, the decision
layer, the LLM and the TTS provider add up to 1.5–4 s on telephony, and the
first reply of a call is the slowest (cold decision/LLM/knowledge paths). A
human agent is never that silent. `latency_fillers` (on by default, under the
`breathing` family master and the layer master) plays a short breath from pre-rendered audio when a dispatched
reply has not started speaking `latency_filler_delay_ms` (default 1500,
500–5000) after the caller stopped speaking, measured from the physical end of
speech the latency probe recorded (dispatch time when unknown).

`breath_gain_db` adjusts only nonverbal breathing clips, including sentence
breaths. It accepts -24 to 0 dB and defaults to 0 (original clip level).
For example, a bot override of -6 dB halves the waveform amplitude while
preserving speech, acknowledgements and voiced thinking cues. Studio exposes
this as **Breathing volume (dB)** under **Advanced tuning**; clip previews
use the bot's saved setting. This changes loudness, not the clip's voice or
tone: synthesized breaths remain gender-matched rather than voice-specific.

Rules, in priority order (`voice_runtime/latency_filler.py`):

- **Response priority and owned cleanup.** Filler streams as owned 20 ms
  chunks with no look-ahead. The first `TTSAudioRawFrame` containing a
  complete PCM sample retires its owner at processor ingress. The producer
  stops, matching queued filler is discarded, and no extra cutoff chunk is
  appended. Ownership survives producer completion and transport chunking;
  filler never enters the response's partial PCM buffer or streaming
  resampler. Fast replies before the configured deadline still skip filler.
  Browser packets carry a unique call/turn token; `filler_clear` stops only
  its sources, with at most a 2 ms fade for a currently sounding source and
  no added scheduling lead before the response. Device-rendered samples
  and already-sent telephony packets remain outside server cleanup.
- **Invisible to turn bookkeeping.** Chunks subclass `OutputAudioRawFrame`,
  which pipecat's output transport does not treat as bot speech, so no
  `BotStartedSpeakingFrame` fires: latency spans, the barge-in/merge
  discriminator, the word-confirmed barge-in gate and the audio gate's echo
  guard all see a quiet bot, and a caller who talks over a breath opens a turn
  exactly as over silence. Nothing is spoken, so history, turn records and
  the client transcript never contain it.
- **One schedule per dispatched turn.** The brain arms the processor at
  dispatch and every cancellation path (barge-in, late-final merge, hang-up,
  teardown) disarms it; caller speech, interruptions and reply audio passing
  through cut it too. A latency acknowledgement uses this same schedule and
  emits plain output PCM, so it neither disarms the filler as fake reply audio
  nor needs a `BotStoppedSpeakingFrame` to re-arm it. A rung whose deadline
  falls while the previous reply's tail is
  still audible is **deferred** (`latency_filler_deferred`) to the bot's next
  silence plus the same gap, not skipped.
- **Telephony packetization.** Owned filler bypasses the FreeSWITCH/Vaani
  200 ms speech buffer and first-packet ramp, leaving as individual 20 ms
  native packets. It cannot be prepended to response PCM. Its tagged
  completion marker does not flush unrelated speech; legacy untagged flushes
  retain their behavior. Third-party serializers receive filler resampled
  separately to native 8 kHz, without contaminating response resampler
  history. No stream-wide `killAudio`/`clear` is sent for filler cleanup:
  these APIs cannot selectively revoke one owner's remote audio and could
  remove valid speech. Packets already accepted by the socket, remote jitter
  buffers and device playback therefore remain an unavoidable boundary.
- **Escalation ladder on long waits** (`latency_filler_ladder`, on by default,
  under the `filler_words` family master — it needs no breath;
  `voice_runtime/voiced_cues.py`). When the reply is
  still not speaking, a short "Hmm…" in the bot's OWN voice follows at
  `latency_filler_hmm_ms` (default 3500, 2000–8000) and a spoken "एक सेकंड…"
  at `latency_filler_spoken_ms` (default 5000, 3000–12000), both measured
  from the caller's end of speech with at least 1 s of quiet between rungs.
  `TTSStartedFrame` does not suppress a rung while playable audio is still
  pending. The TTS router withholds the in-reply sentence inhale for 6 s after
  a pre-reply BREATH started (`sentence_breath_suppressed`, `recent_latency_filler`);
  a spoken acknowledgement or cue does not count as a breath.
  Cue texts are fixed per language (`ladder_cue`), gender-neutral, rendered
  ONCE per (provider, model, voice, language) through the provider's REST
  `synthesize`, trimmed of lead/tail silence, faded, normalized under the reply's level (≈−25 dBFS RMS, peaks ≤ −10 dBFS) and
  cached in memory and as WAV under `filler_audio_dir/cache/`; rendering
  starts in the background when a voice is first armed, and a cue that is not
  ready yet is skipped for that turn (`no_clip`) — the ladder never waits on
  a render, never bills a per-turn TTS call, and a failed render is remembered
  for five minutes. Cues are plain output audio like the breath (no
  bot-speaking flips, fully interruptible, a `TTSAudioRawFrame` mid-cue
  cancels it); because a voiced cue is loud enough to echo, the processor opens
  the caller audio gate's backchannel shield for its duration. The spoken rung
  is withheld (`spoken_withheld`) when the caller's words carry critical
  content (amounts, identifiers, OTPs, dates), a serious caller state
  (complaint, refusal, hardship…) or an identifier capture is open — the
  reply itself must be the next thing such a caller hears. The mock TTS
  provider never renders cues.
- **Gender-matched.** The clip follows the catalog gender of the voice the TTS
  router will actually use for the current conversation language. Operators
  may drop 16-bit PCM WAV recordings into `filler_audio_dir`
  (default `storage/filler_audio`) named with a gender token —
  `filler_male_1.wav`, `breath_female.wav`, `filler_neutral.wav`; all files of
  a gender rotate. A gender without files gets the runtime's synthesized
  breath (three deterministic variants, ~0.7–0.95 s, −30 dBFS, darker for male
  voices, front-loaded so a reply landing 200–300 ms in still cuts an audible
  breath); `python scripts/export_filler_audio.py` writes those as WAVs for
  audition. A file that fails to decode falls back to the synthesized breath.
  Recordings in `filler_audio_dir/optional/` appear in the catalog with
  `requiresSelection: true` and IDs such as
  `file:optional:breath_male_soft_recorded.wav`. They play only when a bot
  selects them, so adding a recording there leaves all default rotations
  unchanged. See `storage/filler_audio/README.md` for source and processing
  details of the bundled soft recorded breath.
- **Choosing the sounds (Natural Conversation tab).** The library holds four
  sound kinds — `breath` (soft, trailing off), `inhale` (short, rising; also
  the in-reply sentence breath), `exhale` (quick onset, long soft tail) and
  `inhale_exhale` (a full quiet cycle) — each per gender, from operator
  recordings named with the kind and gender token (`exhale_female_2.wav`,
  `inhale_exhale_male.wav`) or synthesized. `latency_filler_kind` picks which
  kind covers the pre-reply gap (default `breath`; the ladder's first rung
  keeps its `breath` name in events, with `sound`/`clip` saying what played).
  `filler_audio_selection` — `{kind: {gender: {primary, alternates}}}` —
  narrows a kind/gender to chosen clips: the primary plays first in a call and
  the alternates rotate with it, never the same clip twice in a row while
  more than one is selected; empty means the gender's default clips rotate
  (excluding recordings marked as requiring selection).
  Runtime eligibility always follows the active voice's catalog gender: a
  selection naming another gender's or kind's clips is ignored (the voice's
  own clips rotate) and logged once. `latency_filler_cue_selection` —
  `{lang: {primary, alternates}}` — says which of the "hmm" rung's voiced cue
  texts a bot MAY use (`ladder_cue_options`: Hindi Hmm… / हूँ… / अच्छा… / जी… /
  ठीक है… / उँ-हूँ… / ओह…, English Hmm… / Mm-hmm… / Okay… / Right… / I see… /
  Oh…; primary = the neutral default, alternates = the rest of the allowed
  set; with no selection the whole pool is allowed). The Studio's Natural
  Conversation tab lists every clip and cue with a Play control that streams
  the exact bytes the runtime plays (`GET /bots/{id}/natural-conversation/
  audio`, `…/audio/clip?id=`, `…/audio/cue?language=&kind=&id=` — cues are
  rendered on first preview into the same cache the calls read), a
  male/female/neutral filter (default: the bot's own voice gender) and
  primary/alternate selectors; all three keys are ordinary `humanSpeech`
  overrides (platform → tenant → bot, strict validation on save).
- **Context-aware voiced cues (`SpeechNaturalnessPlanner.plan_latency_cue`).**
  Which cue a long wait actually gets — and whether it gets a word at all —
  is decided per turn when the brain arms the filler, from the caller's own
  words (regex router + signal classifier, no model call), never from a
  fixed sequence. Step one, *is a word needed*: no voiced cue when the turn
  already got a dispatch-time acknowledgement (one voice, not two — the
  re-arm after "जी…" carries `allow_voiced=False`), when the caller dictated
  critical content (amounts, identifiers, OTPs), when the reply is expected
  quickly (speculative decision already done), when the language has no
  pool, and on a share of turns by `latency_cue_probability` (default 0.7,
  halved in a serious caller state, boosted on the first reply) — most long
  waits stay a breath, which is the right filler when only latency needs
  covering. Step two, *which word*: the bot's allowed cues are ranked by the
  ROLE each conveys (`_CUE_ROLES`: thinking Hmm…/हूँ…, information अच्छा…,
  confirm ठीक है…, polite जी…, positive_ack उँ-हूँ…, concern ओह…) against the
  turn context the brain derived (`_latency_cue_context`): knowledge/tool
  path → `lookup`; a question → `thinking`; trouble in the words (complaint,
  hardship, "नहीं मिला", "कट गया", "problem") → `concern`; an answer to the
  workflow's question → `confirm`; an agreement → `affirm`; a stated fact or
  commitment → `information`; courtesy words → `polite`; a longer statement →
  `information`; else `neutral`. Roles a context does not list are never
  voiced there (no "ठीक है…" after a question or after a free statement the
  bot has not accepted — `information` ranks information / polite / thinking
  only — and no "ओह…" after a plain
  statement); in a serious caller state only thinking / polite / concern
  survive, so nothing sounds like agreement with a complaint; "ओह…" is a
  reaction, not a filler — at most once per call and on a minority of
  concern turns. The previously voiced cue (`last_cue_played`) never leads
  again while an alternative exists. The processor receives the ranking as
  a preference order and plays the first cue already rendered
  (`voiced_withheld` in `latency_filler_skipped` when the plan said no word;
  `latency_cue_planned` records context/verbal/reason/role/first per turn).
  The same rule set governs the dispatch-time acknowledgement
  (`plan_early_ack`: answer/question/lookup/neutral pools, no consecutive
  turns, probability), so the two never stack.

Telemetry on the conversation event stream, every event carrying `rung`
(`breath` | `hmm` | `wait`): `latency_filler_played` (`turn`, `gender`,
`waited_ms`, `clip_ms`), `latency_filler_cut` (`reason` `tts_audio` |
`caller_speech` | `interruption` | `bot_speaking` | `early_ack` | brain
cancellation reason, `played_ms`), `latency_filler_completed`,
`latency_filler_deferred` (`bot_speaking`) and `latency_filler_skipped`
(`no_clip` | `spoken_withheld` | `voiced_withheld` | `after_early_ack` |
`breathing_off`). The per-turn `naturalness_trace` log carries
`latency_filler_enabled`, `breathing_enabled`, `filler_words_enabled` and
`latency_fillers_played` (all rungs); the processor's `rungs_played` counts
per kind.
