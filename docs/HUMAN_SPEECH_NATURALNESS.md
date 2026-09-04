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

## Dispatch-time acknowledgements

A person answers a closed turn with a short "जी…" within about a second —
before they know what they will say. EchoSphere does the same at dispatch, the
moment the caller's turn closes: one short token, spoken in its own TTS
envelope, always separate from the reply and never glued to its front (a
preface that waits for the decision layer arrives too late to bridge anything
and only delays the answer). The token follows what the caller just did,
derived deterministically from their words with no model call:

| Caller just… | Context | Tokens (hi) |
|---|---|---|
| gave an answer or a statement | `answer` | "जी…", "ठीक है…", "अच्छा…", "अच्छा, ठीक है…" |
| asked a question | `question` | "हम्म…", "जी…", "अच्छा…" — never "ठीक है", which would sound like an answer |
| asked something the knowledge base answers | `lookup` | "एक सेकंड…", "देख रहा/रही हूँ…" |
| is in a serious state (complaint, refusal, hardship, wrong person, agent request) or dictated amounts/identifiers | `neutral` | "जी…", "हम्म…" only, at half probability — "ठीक है" after a refusal reads as acceptance |

Control: `acknowledgements` on/off; `acknowledgement_probability` (default 0.5,
×1.5 on the first reply after the greeting, the slowest turn of a call); a
hard rule that no two consecutive turns get one (no call opens every reply with
"जी"); pool rotation with no-repeat; exactly one token, never stacked; nothing
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
hoon…"), spoken right before the lookup runs; when a dispatch acknowledgement
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
master switch) plays a short breath from pre-rendered audio when a dispatched
reply has not started speaking `latency_filler_delay_ms` (default 1500,
500–5000) after the caller stopped speaking, measured from the physical end of
speech the latency probe recorded (dispatch time when unknown).

Rules, in priority order (`voice_runtime/latency_filler.py`):

- **Never delays the reply.** The clip streams to the transport in 20 ms
  chunks at real-time pace with two chunks of lead, so at most ~40 ms of
  breath is ever queued ahead of reply audio. The processor sits between the
  TTS service and the output transport and cuts the breath the instant the
  first `TTSAudioRawFrame` passes through, adding one 20 ms taper chunk so
  the breath ends as a breath rather than a click; replies that start before
  the deadline never get a filler at all.
- **Invisible to turn bookkeeping.** Chunks are plain `OutputAudioRawFrame`s,
  which pipecat's output transport does not treat as bot speech, so no
  `BotStartedSpeakingFrame` fires: latency spans, the barge-in/merge
  discriminator, the word-confirmed barge-in gate and the audio gate's echo
  guard all see a quiet bot, and a caller who talks over a breath opens a turn
  exactly as over silence. Nothing is spoken, so history, turn records and
  the client transcript never contain it.
- **One schedule per dispatched turn.** The brain arms the processor at
  dispatch and every cancellation path (barge-in, late-final merge, hang-up,
  teardown) disarms it; caller speech, interruptions and reply audio passing
  through cut it too. Two cases keep the wait covered instead of dropping it:
  a dispatch-time acknowledgement ("जी…", TTS audio like the reply) stands the
  filler down (`early_ack`) and the brain re-arms it the moment the
  acknowledgement's `BotStoppedSpeakingFrame` arrives, if the reply is still
  generating and has produced no audio (`resume`: first rung held ~0.7 s off
  the end of speech, the schedule still anchored on the caller's end of
  speech); and a rung whose deadline falls while the previous reply's tail is
  still audible is **deferred** (`latency_filler_deferred`) to the bot's next
  silence plus the same gap, not skipped.
- **Escalation ladder on long waits** (`latency_filler_ladder`, on by default;
  `voice_runtime/voiced_cues.py`). When the breath has played and the reply is
  still not speaking, a short "हम्म…" in the bot's OWN voice follows at
  `latency_filler_hmm_ms` (default 3500, 2000–8000) and a spoken "एक सेकंड…"
  at `latency_filler_spoken_ms` (default 5000, 3000–12000), both measured
  from the caller's end of speech with at least 1 s of quiet between rungs. Once the reply's synthesis is requested (`TTSStartedFrame`) no new rung starts (`reply_imminent`) — a cue chopped 200 ms in by the reply is a grunt, not a cue — and the TTS router withholds the in-reply sentence inhale for 6 s after any latency rung started (`sentence_breath_suppressed`, `recent_latency_filler`), so a reply never carries two breaths back to back.
  Cue texts are fixed per language (`ladder_cue`), gender-neutral, rendered
  ONCE per (provider, model, voice, language) through the provider's REST
  `synthesize`, trimmed of lead/tail silence, faded, normalized under the reply's level (≈−25 dBFS RMS, peaks ≤ −10 dBFS) and
  cached in memory and as WAV under `filler_audio_dir/cache/`; rendering
  starts in the background when a voice is first armed, and a cue that is not
  ready yet is skipped for that turn (`no_clip`) — the ladder never waits on
  a render, never bills a per-turn TTS call, and a failed render is remembered
  for five minutes. Cues are plain output audio like the breath (no
  bot-speaking flips, fully interruptible, a `TTSAudioRawFrame` mid-cue
  tapers it); because a voiced cue is loud enough to echo, the processor opens
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

Telemetry on the conversation event stream, every event carrying `rung`
(`breath` | `hmm` | `wait`): `latency_filler_played` (`turn`, `gender`,
`waited_ms`, `clip_ms`), `latency_filler_cut` (`reason` `tts_audio` |
`caller_speech` | `interruption` | `bot_speaking` | `early_ack` | brain
cancellation reason, `played_ms`), `latency_filler_completed`,
`latency_filler_deferred` (`bot_speaking`) and `latency_filler_skipped`
(`no_clip` | `spoken_withheld` | `reply_imminent`). The per-turn `naturalness_trace` log carries
`latency_filler_enabled` and `latency_fillers_played` (all rungs); the
processor's `rungs_played` counts per kind.
