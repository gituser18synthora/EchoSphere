"""Render listening samples of every background-ambience preset.

Uses the real runtime loader and mixer (voice_runtime.ambience), so what you
hear is exactly what a caller would get at that preset and volume:

- ``solo/<preset>.wav`` — the room alone, 24 kHz, BOOSTED to -26 dBFS so its
  character is easy to judge on laptop speakers (calls play it far quieter);
- ``in_call/<preset>_browser_vol<V>.wav`` — a bot speech track with the room
  mixed in at volume V, 24 kHz (what the browser test client plays);
- ``in_call/<preset>_phone_vol<V>.wav`` — the same at 8 kHz through a G.711
  mu-law round trip (what a phone caller hears);
- ``index.html`` — players for all of them.

    env/bin/python scripts/render_ambience_samples.py --out DIR --speech bot_speech.wav [--volumes 25,50,100]

``--speech``: any mono 16-bit WAV of bot speech (pauses between sentences let
you hear the room between turns). Without it only the solo files are made.
"""

from __future__ import annotations

import argparse
import html
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.audio.ambience_presets import AMBIENCE_PRESETS, ambience_volume_db  # noqa: E402
from shared.audio.pcm import resample_pcm  # noqa: E402
from voice_runtime import ambience as amb  # noqa: E402


def read_wav(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
            raise SystemExit(f"{path}: need mono 16-bit PCM WAV")
        return wav.readframes(wav.getnframes()), wav.getframerate()


def write_wav(path: Path, pcm: bytes, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)


def mulaw_round_trip(pcm: bytes) -> bytes:
    """G.711-style mu-law (mu=255, 8-bit) encode + decode."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
    mu = 255.0
    y = np.sign(x) * np.log1p(mu * np.abs(x)) / np.log1p(mu)
    q = np.round((y + 1.0) / 2.0 * 255.0) / 255.0 * 2.0 - 1.0
    out = np.sign(q) * (np.power(1.0 + mu, np.abs(q)) - 1.0) / mu
    return np.clip(np.rint(out * 32767.0), -32768, 32767).astype("<i2").tobytes()


def in_call(speech: bytes, preset: str, rate: int, volume: int, lead_s: float = 3.0) -> bytes:
    """Room alone for ``lead_s``, then the speech track, then 3 s of room —
    through the runtime mixer, 40 ms chunks like the transport."""
    bed = amb.load_ambience_bed(rate, preset=preset, level_db=ambience_volume_db(volume))
    mixer = amb.AmbienceMixer(bed, lead_s=0.02, start_offset=0)
    silence = b"\x00\x00" * int(rate * lead_s)
    track = silence + speech + b"\x00\x00" * int(rate * 3.0)
    chunk = int(rate * 0.04) * 2
    return b"".join(mixer.mix(track[i:i + chunk]) for i in range(0, len(track), chunk))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--speech", type=Path)
    parser.add_argument("--volumes", default="50,100")
    args = parser.parse_args(argv)
    volumes = [int(v) for v in args.volumes.split(",") if v.strip()]
    rows = []
    speech24 = speech8 = None
    if args.speech:
        pcm, rate = read_wav(args.speech)
        speech24 = resample_pcm(pcm, rate, 24000)
        speech8 = resample_pcm(pcm, rate, 8000)
    for preset in AMBIENCE_PRESETS.values():
        bed = amb.load_ambience_bed(24000, preset=preset.id)
        solo = bed.samples.astype(np.float64)[: 24000 * 20]
        solo *= (32768 * 10 ** (-26 / 20)) / np.sqrt(np.mean(solo ** 2))
        solo_path = args.out / "solo" / f"{preset.id}.wav"
        write_wav(solo_path, np.clip(np.rint(solo), -32768, 32767).astype("<i2").tobytes(), 24000)
        files = [("solo, boosted to -26 dBFS", solo_path)]
        if speech24 is not None:
            for volume in volumes:
                browser = args.out / "in_call" / f"{preset.id}_browser_vol{volume}.wav"
                write_wav(browser, in_call(speech24, preset.id, 24000, volume), 24000)
                phone = args.out / "in_call" / f"{preset.id}_phone_vol{volume}.wav"
                write_wav(phone, mulaw_round_trip(in_call(speech8, preset.id, 8000, volume)), 8000)
                files += [(f"browser 24 kHz, volume {volume}", browser), (f"phone 8 kHz mu-law, volume {volume}", phone)]
        rows.append((preset, files))
    page = ["<!doctype html><meta charset=utf-8><title>Ambience presets</title>",
            "<style>body{font:15px system-ui;margin:24px;max-width:900px}h2{margin:24px 0 4px}"
            "p{margin:0 0 8px;color:#555}div{margin:4px 0}span{display:inline-block;width:230px}</style>",
            "<h1>Background ambience presets</h1>",
            "<p>In-call files use the real runtime mixer: 3 s of room, the bot speaking, 3 s of room. "
            "Volume 50 is the default.</p>"]
    for preset, files in rows:
        page.append(f"<h2>{html.escape(preset.label)} <code>{preset.id}</code></h2><p>{html.escape(preset.description)}</p>")
        for label, path in files:
            rel = path.relative_to(args.out).as_posix()
            page.append(f"<div><span>{html.escape(label)}</span><audio controls preload=none src='{rel}'></audio></div>")
    (args.out / "index.html").write_text("\n".join(page), encoding="utf-8")
    for preset, files in rows:
        for label, path in files:
            print(f"{preset.id:13s} {label:28s} {path}")
    print(f"index: {args.out / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
