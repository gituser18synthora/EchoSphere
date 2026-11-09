# Background interruption investigation: local implementation and evidence

Status: local code and tooling are ready for controlled-call validation. This is **not a validated production preset**. No live/demo settings, production configuration, server code or services were changed or restarted.

## Available evidence

- Twenty stereo WAV clips from six recent calls are extracted in ignored local storage (`storage/tmp/background_replay/clips`). The manifest records source call IDs, padded timestamp bounds, SHA-256 hashes, transcript and acoustic evidence.
- `incident_later` is the one user-confirmed background segment: “बाद में”, −48 dBFS against a −28.3 dBFS reference, or −19.7 dB. The recorded sustained-speech interruption occurred at 0.82 seconds. Both “अभी” events are included as **unverified background candidates**.
- Normal/soft/overlap and call-start review candidates are present. Device identity is unknown. Two longer windows are provided for continuity review; they do not establish continuous background conversation. The requested third confirmed continuous-background example and verified normal/soft/speakerphone positive coverage are pending controlled calls or listening review.
- A separate seven-day read-only export scanned 172 recorded calls for the same tenant/bot, from 2026-11-02 08:12:54 UTC through 2026-11-09 08:12:54 UTC. There are 242 candidate events and a 24-item listening shortlist. These event counts are **not** counts of verified background interruptions. Temporal event associations can repeat or be uncertain.
- New shortlist labels remain unverified. An existing confirmed incident in the replay manifest does not establish the speakers in other segments of its call.

The harness replays recorded transcripts, level measurements and timing evidence through the real caller-level and interruption policies. It **does not rerun the WAV through VAD/STT**, identify a speaker by listening, or reconstruct the complete transport/controller timeline. Two observed/replay disagreement cases are explicitly retained in [BASELINE.md](BASELINE.md). Independent automatic transcription was not used as ground truth.

## Baseline and comparison

The full pre-change 20-row baseline is in [BASELINE.md](BASELINE.md). With guard OFF the confirmed background segment is classified as caller, accepted and interrupts. Quiet “अभी” segments are classified as suspects but accepted in shadow mode.

All 32 combinations of the five relevant booleans are crossed with allowance candidates 0/4/6/8/12 dB and meaningful-word counts 2/3: **320 configurations, 6,400 real-clip policy evaluations**. Base margin stays at 12 dB; hold-limit boundaries 0/1/2/5 are separately tested. Continuous numeric ranges are sampled rather than exhaustively enumerated.

[VALIDATION.md](VALIDATION.md) contains a row for every configuration; [validation_clips.csv](validation_clips.csv) gives every clip result; [validation.json](validation.json) contains the synthetic and repeat-stress controls. Transcript persistence does not affect interruption results, so configurations differing only in that flag intentionally share policy metrics.

| Local comparison | Confirmed background blocked | Background interrupts | Provisional caller candidate inputs accepted | Synthetic caller overlap at 0 / −8 / −15 / −20 dB |
|---|---|---|---|---|
| Legacy: guard OFF, allowance 12 | 0/1 | 1 | 11/11 | allow / allow / allow / allow |
| Guard ON alone, allowance 12 | 0/1 | 1 | 9/11 | allow / allow / allow / allow |
| Guard ON, transcript required N=2, fail-open OFF, allowance 0 | 1/1 | 0 | 9/11 | allow / allow / hold / hold |
| Same, allowance 4 | 1/1 | 0 | 9/11 | allow / allow / allow / hold |
| Same, allowance 6 | 1/1 | 0 | 9/11 | allow / allow / allow / hold |
| Same, allowance 8 | 0/1 | 1 | 9/11 | allow / allow / allow / hold |
| Same, allowance 12 | 0/1 | 1 | 9/11 | allow / allow / allow / allow |
| Allowance 6 plus startup protection | 1/1 | 0 | 7/11 | allow / allow / allow / hold |

“Blocked” requires both no interruption and no accepted background input. Synthetic controls perturb logged acoustic evidence and use a three-word transcript; they are not new audio recordings or verified soft/speakerphone callers. Candidate input acceptance is not genuine interruption recall.

**Genuine allowed and false-positive metrics are N/A** because there are no verified positive interruption clips yet. A false positive in the generated tables means a verified genuine interruption incorrectly held. The validator will compute these counts once explicitly confirmed positive segments are supplied. A call-level scenario alone does not silently label every utterance in that recording.

## Local code changes and flags

All defaults retain the previous policy. Flags are resolved through the existing tenant/transport schema; no saved settings were edited.

| Flag | Default | Local effect / proposed offline trial |
|---|---|---|
| `background_speech_guard` | OFF | Existing enforcement is retained; trial ON together with a tested margin, not alone. |
| `background_speech_margin_db` | 12 | Keep 12 for comparisons; it is part of the total threshold. |
| `background_bot_audio_allowance_db` | 12 | Compare 4 and 6 as candidates; **6 is not a final tuned value**. |
| `barge_in_require_transcript` | OFF | Trial ON: neither sustained audio nor a provisional pause can commit an interruption without transcript words. |
| `barge_in_meaningful_words` | 2 | Trial 2; punctuation-only tokens and recording announcements do not count. Stable interim or final transcript evidence can qualify. Compare 3 separately. |
| `background_start_protection` | OFF | Test both values. ON holds overlap until a trusted caller reference exists, including genuine early callers. Quiet-bot answers can establish the reference. |
| `background_quiet_failopen_enabled` | ON | Trial OFF for repeated-background stress. ON preserves the availability fallback for a soft caller. |
| `background_quiet_max_holds` | 2 | Configurable; legacy accepts the third consecutive quiet suspect. Ignored when fail-open is OFF. |
| `transcript_playout_only` | OFF | Experimental opt-in: persist conservative transport-confirmed TTS prefixes, with explicit delivery metadata. Validate independently. |

Why compare 4/6: the old effective threshold is 12+12=24 dB, so a segment only 19.7 dB below reference passes. At allowance 6, the total is 18 dB and it is held. At allowance 8, the total is 20 dB and it passes again. Allowance 0 rejects the −15 dB synthetic caller control; 4 and 6 preserve it. This narrow set does not establish which candidate is safe for real soft voices or speakerphones.

Meaningful tokens include Hindi words, numbers and business terms; this is a lexical threshold, not an intent or speaker-identity classifier. Requiring N=2 still allows a two-word “बाद में” transcript if its level is accepted. The combined guard is necessary for this incident.

The transcript-required option disables provisional ducking so audio alone cannot visibly pause or cancel the reply. This trades immediate ducking for STT confirmation latency. Quiet-bot turn starts retain the fast path.

## Transcript correctness and remaining gaps

With the experimental persistence flag, provider-completed text boundaries travel in the audio queue and are committed after output. Evidence accounts for transport re-chunking that drops context IDs, resampling, tail flushing, cancellation and excluded filler. Completion evidence follows `TTSStoppedFrame`, which flushes the transport tail. Generated text is not shortened using a character/time ratio.

Only confirmed prefixes are written in stored bot rows. Unverified text becomes an empty row with `delivery.status=unverified`; a confirmed prefix is marked `transport_delivered_prefix` and `partial_sentence_omitted=true`. Caller rows and default transcripts retain their prior shape. Billing still uses the existing generated/dispatched usage basis.

Material limits:

- A provider-completed prefix proves transport handoff, **not far-end hardware playback**. Metadata explicitly sets `far_end_playback_verified=false`.
- Exact interrupted partial-sentence words need word/audio alignment or playback acknowledgement. They are currently omitted rather than guessed. With streaming mode that has no per-sentence provider final, an interrupted generation can have no provable prefix at all.
- The legacy `EchoTTSService` REST path does not yet emit these new proof frames; opt-in transcript rows on that path are unverified/empty. The experimental persistence flag should not be recommended for that path as complete support.
- Strict matching can omit text transformed before synthesis, or fail to map multiple/repeated generation contexts to a recorded turn. It fails conservatively; this is not a complete word-aligned transcript solution. In-memory LLM history and transient client `bot_text` remain generated text; the change concerns persisted conversation rows.
- New interruption controls are wired into the local `WordConfirmedBargeInStrategy` path used for these replays. Provider-owned turn modes (Deepgram Flux and Sarvam server-VAD endpointing) bypass that strategy and still need corresponding provider-adapter enforcement and tests. Do not infer parity across those modes from this table. The brain's startup/quiet-suspect transcript policy still applies when a caller-level source is available, but cannot undo an earlier provider interruption.
- Startup protection requires an audio gate capable of establishing the caller reference. With no level source, the opted-in conservative strategy cannot establish a reference and will keep overlap held.

## Known policy failures / tradeoffs

1. **Loud third-person speech passes.** A four-word background segment at the caller's level passes every tested configuration once a reference exists. Relative amplitude plus a word count is not speaker identity. Caller voice consistency is not completed or claimed to be enforced by this change.
2. **Very soft callers can be held.** At allowance 6 a genuine −20 dB attenuation control is indistinguishable from the confirmed background level and is rejected. The two soft review candidates also become held inputs with guard enforcement, but their actual speakers are not verified.
3. **Startup protection holds genuine early callers.** The synthetic no-reference caller is allowed with the flag OFF and held with it ON. Two provisional candidate inputs are additionally held in the clip set.
4. **Short interruptions can be lost/delayed.** One-word “हाँ”, “रुको”, “Stop”, or a two-word genuine interruption when N=3, cannot meet that transcript threshold. STT failure/late finals add delay. No real caller latency improvement is claimed.
5. **Fail-closed can lock out a changed caller level.** The old repeated-quiet-suspect pattern is hold/hold/accept/hold/hold. Disabling fallback yields five holds. This blocks repeated background evidence but can also prevent a soft caller from rebasing their reference.
6. **Identifier capture retains its existing level-guard exemption.** This avoids corrupting dictated digits; it is not a universal third-person filter in every workflow state. Startup protection has its separate conservative overlap rule.
7. Confirmed speakerphone, soft-caller and continuous-background success rates remain unavailable. No ambiguous waveform/transcript is promoted to ground truth.

## Checks completed

- Original three-file suite: **43 passed**, with no original tests deleted, weakened or edited.
- Additional focused backend regression run: **263 passed, 1 skipped**. Includes private-WAV integrity, frozen default baseline, meaningful transcript/VAD behavior, actual brain startup and repeated-suspect policy, transport chunking/resampling/tail/cancellation, TTS and turn-taking regressions. The skipped speaker-model integration check lacks optional local assets; speaker identity enforcement is not claimed validated. The run also reports dependency deprecations and pending silence-watch tasks from test teardown; it is not warning-free.
- Turn Detection UI/schema transfer: **31 passed** across two files.
- `npm run build`: passed, with the existing large-bundle advisory.
- `git diff --check`: passed.

The real-transport tests use a local websocket playback model, not phone hardware. No test call, configuration application, deployment or remote service restart was performed.

## Next validation input

Controlled calls are being made by the owner with current demo settings. Preserve the old-settings recordings as baseline evidence. Add each call ID/scenario, then confirm the relevant interruption or background windows; do not treat all speech in the call as the same speaker. Re-run the same local policy matrix and report soft/speakerphone recall, startup tradeoffs and continuous-background behavior. Keep unclear segments explicitly unclear.

[REVIEW_SHORTLIST.md](REVIEW_SHORTLIST.md) is the listening handoff. [WORKFLOW.md](WORKFLOW.md) documents the local import/extraction/replay commands. No production-ready recommendation should be finalized until those labels and the provider/transcript support gaps above are addressed.
