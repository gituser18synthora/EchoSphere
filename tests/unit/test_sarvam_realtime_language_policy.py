"""Sarvam realtime STT handshake: language pinning and transcription mode.

Live call cv_a3f4eee0d0d2 (2026-10-22): with ``language_code=auto`` the
realtime socket rendered the caller's Hindi "haan boliye" as English ("Yes,
speak."); replaying the recording on 2026-10-23 gave "Hello, speak." under
auto and the Devanagari transcript under ``hi-IN``. A bot with one configured
language is therefore always pinned to it on this socket, whatever its
auto-detect setting says, and the socket is always asked for transcription
(never ``translate``/``translit``/``codemix``). A multi-language bot keeps
auto-detect — and inherits the English rendering of short Hindi answers.
"""

from __future__ import annotations

import pytest

from shared.bot_config import ResolvedBotConfig
from shared.config import get_settings
from voice_runtime.pipeline import build_stt_service


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def add_event(self, kind: str, **data) -> None:
        self.events.append((kind, data))

    def of(self, kind: str) -> list[dict]:
        return [d for k, d in self.events if k == kind]


# ── handshake language policy ───────────────────────────────────────────────

def _config(languages: list[str], *, default: str = "hi-IN", settings: dict | None = None,
            stt_language: str = "") -> ResolvedBotConfig:
    return ResolvedBotConfig(
        tenant_id="t", bot_id="b", bot_name="Test", version="1", published=True,
        language=default, languages=languages,
        stt={
            "provider": "sarvam", "model": "saaras:v3", "language": stt_language,
            "api_key_reference": "env:TEST_SARVAM_API_KEY",
            "settings": settings if settings is not None else {},
        },
    )


@pytest.fixture
def realtime_on(monkeypatch):
    monkeypatch.setenv("TEST_SARVAM_API_KEY", "k")
    settings = get_settings()
    monkeypatch.setattr(settings, "sarvam_realtime_stt", True)
    monkeypatch.setattr(settings, "sarvam_realtime_endpointing", "manual")
    monkeypatch.setattr(settings, "sarvam_realtime_stream_type", "balanced")
    return settings


class TestHandshakeLanguagePolicy:
    async def test_single_language_hindi_bot_is_pinned_and_transcribes(self, realtime_on):
        recorder = _Recorder()
        service = build_stt_service(
            _config(["hi-IN"], settings={"auto_detect_language": True, "mode": "translate"}),
            recorder=recorder,
        )
        try:
            params = service._query_params()
            assert params["language_code"] == "hi-IN"
            assert params["mode"] == "transcribe"
            assert params["model"] == "saaras:v3-realtime" and params["endpointing"] == "manual"
            [event] = recorder.of("stt_realtime")
            assert event["language"] == "hi-IN" and event["language_pinned"] == "single_language_bot"
            assert event["mode"] == "transcribe" and event["mode_forced"] is True
            # The legacy-path policy still says auto (explicit setting) — only
            # the realtime socket overrides it.
            assert recorder.of("stt_language_mode")[0]["mode"] == "auto"
        finally:
            await service.cleanup()

    async def test_multi_language_bot_keeps_auto_detect(self, realtime_on):
        recorder = _Recorder()
        service = build_stt_service(_config(["hi-IN", "en-IN"], settings={"mode": "transcribe"}), recorder=recorder)
        try:
            assert service._query_params()["language_code"] == "auto"
            [event] = recorder.of("stt_realtime")
            assert event["language"] == "auto" and event["language_pinned"] is None
            assert event["mode_forced"] is False
        finally:
            await service.cleanup()

    async def test_an_explicit_stt_language_pins_any_bot(self, realtime_on):
        service = build_stt_service(_config(["hi-IN", "en-IN"], stt_language="en-IN"))
        try:
            assert service._query_params()["language_code"] == "en-IN"
        finally:
            await service.cleanup()


