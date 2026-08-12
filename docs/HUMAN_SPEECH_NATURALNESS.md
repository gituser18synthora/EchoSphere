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

## Response start and backchannel evidence

EchoSphere adds no artificial response-start delay. Simple replies use normal
streaming immediately; eligible generic tool lookups may speak a short,
unambiguous acknowledgement while the lookup runs. Conversational rhythm comes
from real processing, sentence aggregation and planned TTS gaps.

A backchannel requires positive evidence that the caller still owns the floor.
When the audio gate is present, at least 250 ms of current live speech is
required. Otherwise, the open VAD/provider turn (`UserStartedSpeaking` without a
matching stop) is the strongest available live-speech signal. Serious trusted
caller states (complaint, hardship, refusal, wrong person, agent request,
distress or frustration) suppress casual backchannels without an extra model
call. Backchannels never close a caller turn or enter semantic history.
