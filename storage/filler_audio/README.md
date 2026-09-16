# Latency filler audio

Optional operator recordings for the human-speech `latency_fillers` feature
(see `docs/HUMAN_SPEECH_NATURALNESS.md`). Drop 16-bit PCM WAV files here, named
with a gender token, e.g. `filler_male_1.wav`, `breath_female.wav`,
`filler_neutral.wav` for the pre-reply breath, `inhale_male.wav` / `inhale_female.wav` for the short
rising breath (also used before a sentence inside a reply), `exhale_<gender>.wav`
for a settling exhale and `inhale_exhale_<gender>.wav` for a full quiet breath
cycle. Which kind covers the gap before a reply, and which files of it a bot may
play (primary + alternates), is chosen per bot in the Natural Conversation tab
(`humanSpeech.latency_filler_kind` / `filler_audio_selection`); with no
selection all files of a gender rotate; a kind/gender with no file uses the
runtime's synthesized sound. Every file can be auditioned from that tab (the
API serves the exact bytes the runtime plays). Any sample rate is accepted (resampled per
call).

## Recordings selected per bot

Put recordings in `optional/` to offer them in the catalog without changing
the default rotation for existing bots. They use IDs such as
`file:optional:breath_male_soft_recorded.wav` and play only when explicitly
chosen as a primary or alternate. The same kind/gender filename rules apply.
An empty or invalid selection falls back to the original default rotation.
Restart the local API and voice worker after adding or changing assets;
each process scans and renders them once.

Other subdirectories are ignored — `python scripts/export_filler_audio.py`
writes the synthesized defaults into `synthesized/` for audition.

`cache/` holds the voiced ladder cues ("हम्म…", "एक सेकंड…") the runtime renders
once per provider/model/voice/language (`voice_runtime/voiced_cues.py`); delete a
file there to force a re-render after a voice change. Both subdirectories are
git-ignored.

### Soft recorded breath

`optional/breath_male_soft_recorded.wav` is derived from
[Normal soft breathing by therisingorder](https://freesound.org/people/therisingorder/sounds/265040/),
released under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/).
Source downloaded on 2026-09-15 from the publisher's
[HQ preview](https://cdn.freesound.org/previews/265/265040_2196252-hq.mp3).
The source does not identify the performer's gender; `male` is the intended
bot voice playback bucket, not a claim about the performer. This is a generic
human recording, not a recording of the bot's TTS speaker.

Processing: decode to 24 kHz mono; second-order Butterworth high-pass at
120 Hz and low-pass at 4500 Hz (zero phase); gentle spectral noise reduction
using source 4.0–5.5 s as the room-noise reference (512-sample STFT, 384-sample
overlap, gain floor 0.4); crop 5.72–6.48 s; apply 30 ms raised-cosine edge
fades; normalize the loudest sliding 100 ms RMS window to −31 dBFS; export
16-bit PCM WAV. Duration: 760 ms. No pitch shift or time stretching.
With `breath_gain_db: -6`, the loudest 100 ms window is approximately −37 dBFS.

Source SHA-256: `3c7fb3fd174e0898bdb9c0e85450e66f0f10859bff76ad6c95a84c4cf556adca`.
Asset SHA-256: `c973a1f4782194595a527d95828d477bc85a988d6077371ddb146b82e2d109c9`.
