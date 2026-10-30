"""Tenant-scoped storage for voice-clone source audio.

Layout: <VOICE_CLONE_AUDIO_DIR>/<tenant_id>/<voice_id>/<sample_id>.<ext>
Every path segment is server-generated (tenant/voice/sample ids), so
user-controlled filenames can never form the on-disk path — path-traversal
safe by construction, same contract as shared/knowledge/ingestion/storage.py.
Extensions are whitelisted upstream (voice_clones._read_samples) before
anything reaches save_sample.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import wave
from pathlib import Path

from shared.config import get_settings

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_SAFE_EXTENSION = re.compile(r"^[a-z0-9]{1,8}$")
_PROBE_TIMEOUT_S = 15


class CloneAudioStorageError(ValueError):
    pass


def _root() -> Path:
    root = Path(get_settings().voice_clone_audio_dir)
    if not root.is_absolute():
        project_root = Path(__file__).resolve().parents[2]
        root = project_root / root
    return root


def _validate_segment(value: str, label: str) -> str:
    if not _SAFE_SEGMENT.match(value or ""):
        raise CloneAudioStorageError(f"Invalid {label}")
    return value


def save_sample(
    tenant_id: str, voice_id: str, sample_id: str, extension: str, data: bytes
) -> str:
    """Persist sample bytes; returns the storage path relative to the root."""
    if not _SAFE_EXTENSION.match(extension or ""):
        raise CloneAudioStorageError("Invalid audio extension")
    tenant_dir = _validate_segment(tenant_id, "tenant id")
    voice_dir = _validate_segment(voice_id, "voice id")
    file_name = f"{_validate_segment(sample_id, 'sample id')}.{extension}"

    directory = _root() / tenant_dir / voice_dir
    directory.mkdir(parents=True, exist_ok=True)
    target = (directory / file_name).resolve()
    if not target.is_relative_to(_root().resolve()):
        raise CloneAudioStorageError("Resolved path escapes the storage root")
    target.write_bytes(data)
    return str(Path(tenant_dir) / voice_dir / file_name)


def resolve_sample_path(storage_path: str | None) -> Path | None:
    """Resolve a stored relative path. None when the reference is absent,
    escapes the root (defense against a tampered row) or the file is gone."""
    if not storage_path:
        return None
    root = _root().resolve()
    try:
        target = (root / storage_path).resolve()
    except (OSError, ValueError):
        return None
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target


def delete_sample(storage_path: str | None) -> None:
    """Best-effort removal of a stored sample (and its voice dir if empty)."""
    path = resolve_sample_path(storage_path)
    if path is None:
        return
    try:
        path.unlink()
        parent = path.parent
        if parent != _root().resolve() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass


def normalize_duration_metadata(path: Path) -> float | None:
    """Give a container a proper duration header by remuxing in place with
    ffmpeg -c copy (no re-encode). Chrome MediaRecorder webm/ogg blobs lack
    one, which breaks both server-side duration validation and the seek bar
    of the saved-audio player. Returns the probed duration of the normalized
    file, or None when normalization was not possible (original untouched)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    tmp = path.with_name(path.name + ".norm" + path.suffix)
    try:
        proc = subprocess.run(
            [ffmpeg, "-v", "error", "-y", "-i", str(path), "-c", "copy", str(tmp)],
            capture_output=True, timeout=60, check=False,
        )
        if proc.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0:
            duration = probe_duration_sec(tmp)
            if duration:
                tmp.replace(path)
                return duration
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        tmp.unlink(missing_ok=True)
    return None


def probe_duration_sec(path: Path) -> float | None:
    """Best-effort audio duration from the stored bytes.

    ffprobe when available (covers webm/ogg/mp3/m4a/flac/…), stdlib wave as
    the .wav fallback. None when the duration cannot be determined — e.g.
    Chrome MediaRecorder webm blobs carry no duration header; callers fall
    back to the client-declared value for those.
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            proc = subprocess.run(
                [
                    ffprobe, "-v", "error",
                    "-show_entries", "format=duration:stream=duration",
                    "-of", "json", str(path),
                ],
                capture_output=True, timeout=_PROBE_TIMEOUT_S, check=False,
            )
            if proc.returncode == 0:
                payload = json.loads(proc.stdout or b"{}")
                candidates = [payload.get("format", {}).get("duration")]
                candidates += [s.get("duration") for s in payload.get("streams", [])]
                for raw in candidates:
                    try:
                        value = float(raw)
                    except (TypeError, ValueError):
                        continue
                    if value > 0:
                        return round(value, 2)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as handle:
                rate = handle.getframerate()
                if rate:
                    return round(handle.getnframes() / rate, 2)
        except Exception:  # noqa: BLE001 — malformed containers raise freely
            pass
    return None
