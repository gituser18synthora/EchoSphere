"""Export the synthesized latency-filler breaths as WAV files for audition.

    python scripts/export_filler_audio.py [--out storage/filler_audio/synthesized] [--rate 24000]

Writes ``breath_<gender>_<variant>.wav`` for every gender/variant the runtime
would synthesize. The output directory is a SUBDIRECTORY of the asset
directory on purpose: the runtime scans only top-level ``*.wav`` files of
``filler_audio_dir`` (default ``storage/filler_audio``), so these exports are
never mistaken for operator recordings. To replace the synthesized breath for
a gender, drop 16-bit PCM WAV recordings named ``filler_male_1.wav`` /
``breath_female.wav`` / ``filler_neutral.wav`` into that top-level directory.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.audio.pcm import pcm_to_wav_bytes  # noqa: E402
from voice_runtime.latency_filler import (  # noqa: E402
    _VARIANTS_PER_GENDER,
    GENDERS,
    KINDS,
    synthesize_breath,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default="storage/filler_audio/synthesized")
    parser.add_argument("--rate", type=int, default=24000)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for kind in KINDS:
        for gender in GENDERS:
            for variant in range(_VARIANTS_PER_GENDER):
                pcm = synthesize_breath(gender, args.rate, variant=variant, kind=kind)
                path = out / f"{kind}_{gender}_{variant + 1}.wav"
                path.write_bytes(pcm_to_wav_bytes(pcm, sample_rate=args.rate))
                print(f"{path}  {len(pcm) / (args.rate * 2) * 1000:.0f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
