# Latency filler audio

Optional operator recordings for the human-speech `latency_fillers` feature
(see `docs/HUMAN_SPEECH_NATURALNESS.md`). Drop 16-bit PCM WAV files here, named
with a gender token, e.g. `filler_male_1.wav`, `breath_female.wav`,
`filler_neutral.wav` for the pre-reply breath, and `inhale_male.wav` / `inhale_female.wav`
for the short rising breath before a sentence inside a reply. All files of a gender rotate; a gender with no file uses
the runtime's synthesized breath. Any sample rate is accepted (resampled per
call). Subdirectories are ignored — `python scripts/export_filler_audio.py`
writes the synthesized defaults into `synthesized/` for audition.
