"""Provider factory/config validation, mock providers, PCM + TTS-text utils."""

import base64
import json
import os

import pytest
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
)

from shared.errors import ApiError
from shared.telephony import (
    SUPPORTED_PROVIDERS,
    TelephonyProviderConfig,
    connect_instructions,
)
from shared.providers.base import ProviderConfig, ProviderError
from shared.providers.factory import (
    _REGISTRY,
    clear_provider_cache,
    get_llm_provider,
    get_stt_provider,
    get_tts_provider,
)
from voice_runtime.telephony import VaaniFrameSerializer, build_media_serializer

os.environ.setdefault("FAKE_TEST_KEY", "test-key")
KEY_REF = "env:FAKE_TEST_KEY"
VAANI_START = {
    "event": "start",
    "streamSid": "MZ123",
    "start": {
        "track": "inbound",
        "mediaFormat": {
            "encoding": "audio/lin",
            "sampleRate": 8000,
            "channels": 1,
        },
    },
}


class TestFactory:
    def test_unknown_provider_rejected(self):
        with pytest.raises(ProviderError):
            get_stt_provider(ProviderConfig(provider="nope"))

    def test_mock_providers_construct(self):
        assert get_stt_provider(ProviderConfig(provider="mock"))
        assert get_tts_provider(ProviderConfig(provider="mock"))
        assert get_llm_provider(ProviderConfig(provider="mock"))

    def test_openai_family_requires_key(self, monkeypatch):
        clear_provider_cache()
        monkeypatch.delenv("MISSING_KEY_VAR", raising=False)
        with pytest.raises(ProviderError) as excinfo:
            get_stt_provider(
                ProviderConfig(provider="whisper", api_key_reference="env:MISSING_KEY_VAR")
            )
        assert excinfo.value.category == "auth"
        clear_provider_cache()

    def test_registry_covers_required_kinds(self):
        kinds = {k for k, _ in _REGISTRY}
        assert kinds == {"stt", "tts", "llm"}
        providers = {p for _, p in _REGISTRY}
        for required in ("openai", "deepgram", "assemblyai", "sarvam", "elevenlabs",
                         "azure", "google", "anthropic", "mock"):
            assert required in providers


class TestMockRoundTrip:
    async def test_tts_to_stt(self):
        tts = get_tts_provider(ProviderConfig(provider="mock"))
        stt = get_stt_provider(ProviderConfig(provider="mock"))
        result = await tts.synthesize("hello round trip")
        transcript = await stt.transcribe(result.audio)
        assert transcript.text == "hello round trip"

    async def test_llm_stream_joins_to_generate(self):
        llm = get_llm_provider(ProviderConfig(provider="mock"))
        gen = await llm.generate([{"role": "user", "content": "ping"}])
        streamed = "".join([t async for t in llm.stream([{"role": "user", "content": "ping"}])])
        assert gen.text.strip() == streamed.strip()

    async def test_llm_grounded_mode_quotes_context(self):
        llm = get_llm_provider(ProviderConfig(provider="mock"))
        out = await llm.generate(
            [{"role": "user", "content": "what is the grace period"}],
            system="rules...\nContext:\n[1] The grace period is 30 days.",
        )
        assert "30 days" in out.text


class TestPCM:
    def test_wav_round_trip(self):
        from shared.audio.pcm import pcm_to_wav_bytes, wav_to_pcm

        pcm = bytes(range(256)) * 8
        wav = pcm_to_wav_bytes(pcm, 16000)
        out, rate = wav_to_pcm(wav)
        assert out == pcm and rate == 16000

    def test_resample_halves_length(self):
        from shared.audio.pcm import resample_pcm

        one_second_16k = b"\x00\x01" * 16000
        out = resample_pcm(one_second_16k, 16000, 8000)
        assert len(out) == 16000  # 8000 samples * 2 bytes

    def test_same_rate_passthrough(self):
        from shared.audio.pcm import resample_pcm

        pcm = b"\x01\x02" * 100
        assert resample_pcm(pcm, 16000, 16000) == pcm


class TestTTSText:
    def test_sentence_split_with_abbreviations(self):
        from shared.audio.text import split_into_sentences

        parts = split_into_sentences("Hello world. This is Dr. Smith. Bye.")
        assert len(parts) == 3
        assert parts[1] == "This is Dr. Smith."

    def test_devanagari_danda(self):
        from shared.audio.text import split_into_sentences

        parts = split_into_sentences("नमस्ते। ठीक है।")
        assert len(parts) >= 1

    def test_sanitize_strips_markup(self):
        from shared.audio.text import sanitize_for_tts

        out = sanitize_for_tts("**bold** and `code`")
        assert "*" not in out and "`" not in out


class TestVaaniTelephony:
    def test_supported_provider_catalog_and_connect_payload(self):
        assert "vaani" in SUPPORTED_PROVIDERS
        instructions = connect_instructions(
            "vaani",
            TelephonyProviderConfig(
                provider="vaani", public_ws_base="wss://voice.example.com"
            ),
            "vs_123",
        )
        assert instructions.content_type == "application/json"
        assert json.loads(instructions.body) == {
            "url": "wss://voice.example.com/ws/telephony/vaani/vs_123"
        }

    def test_serializer_start_validation(self):
        serializer = build_media_serializer("vaani", start_message=VAANI_START)
        assert isinstance(serializer, VaaniFrameSerializer)

        bad = {
            **VAANI_START,
            "start": {
                **VAANI_START["start"],
                "mediaFormat": {"sampleRate": 16000, "channels": 1},
            },
        }
        with pytest.raises(ApiError, match="sampleRate"):
            build_media_serializer("vaani", start_message=bad)

    async def test_media_payload_deserializes_to_8khz_pcm(self):
        serializer = VaaniFrameSerializer(stream_sid="MZ123")
        pcm = b"\x01\x02" * 160
        frame = await serializer.deserialize(json.dumps({
            "event": "media",
            "streamSid": "MZ123",
            "media": {
                "chunk": "1",
                "timestamp": "1758021592",
                "payload": base64.b64encode(pcm).decode("ascii"),
            },
        }))
        assert isinstance(frame, InputAudioRawFrame)
        assert frame.audio == pcm
        assert frame.sample_rate == 8000
        assert frame.num_channels == 1

    async def test_outbound_audio_uses_vaani_chunk_rules(self):
        serializer = VaaniFrameSerializer(stream_sid="MZ123")
        assert await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x00" * 1600, sample_rate=8000, num_channels=1,
        )) is None
        raw = await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 1600, sample_rate=8000, num_channels=1,
        ))
        message = json.loads(raw)
        audio = base64.b64decode(message["media"]["payload"])
        assert message["event"] == "media"
        assert message["streamSid"] == "MZ123"
        assert message["media"]["track"] == "inbound"
        assert message["media"]["chunk"] == "1"
        assert len(audio) == 3200
        assert len(audio) % 320 == 0

    async def test_clear_and_transfer_control_events(self):
        serializer = VaaniFrameSerializer(stream_sid="MZ123")
        clear = json.loads(await serializer.serialize(InterruptionFrame()))
        assert clear == {
            "event": "clear",
            "streamSid": "MZ123",
            "clear": {"reason": "interrupt"},
        }

        transfer = json.loads(await serializer.serialize(OutputTransportMessageFrame(message={
            "type": "telephony_control",
            "event": "transfer",
            "reason": "explicit_transfer_request",
        })))
        assert transfer == {
            "event": "transfer",
            "streamSid": "MZ123",
            "transfer": {"reason": "explicit_transfer_request"},
        }

    async def test_short_final_audio_flushes_on_bot_stop(self):
        serializer = VaaniFrameSerializer(stream_sid="MZ123")
        assert await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 640, sample_rate=8000, num_channels=1,
        )) is None
        raw = await serializer.serialize(BotStoppedSpeakingFrame())
        message = json.loads(raw)
        audio = base64.b64decode(message["media"]["payload"])
        assert len(audio) == 640
        assert len(audio) % 320 == 0

    @staticmethod
    def _media(payload_b64: str, *, chunk="1", sid="MZ123") -> str:
        return json.dumps({
            "event": "media", "streamSid": sid,
            "media": {"chunk": chunk, "timestamp": "1758021592",
                      "payload": payload_b64},
        })

    async def test_foreign_stream_sid_is_dropped(self):
        serializer = VaaniFrameSerializer(stream_sid="MZ123")
        pcm64 = base64.b64encode(b"\x01\x02" * 160).decode("ascii")
        assert await serializer.deserialize(self._media(pcm64, sid="MZ999")) is None
        stop = json.dumps({"event": "stop", "streamSid": "MZ999",
                           "stop": {"reason": "stop"}})
        assert await serializer.deserialize(stop) is None
        # the session's own stream still works afterwards
        frame = await serializer.deserialize(self._media(pcm64))
        assert isinstance(frame, InputAudioRawFrame)

    async def test_duplicate_and_replayed_chunks_are_dropped(self):
        serializer = VaaniFrameSerializer(stream_sid="MZ123")
        pcm64 = base64.b64encode(b"\x01\x02" * 160).decode("ascii")
        assert isinstance(await serializer.deserialize(self._media(pcm64, chunk="1")),
                          InputAudioRawFrame)
        # exact duplicate and an older (replayed) chunk are both ignored
        assert await serializer.deserialize(self._media(pcm64, chunk="1")) is None
        assert await serializer.deserialize(self._media(pcm64, chunk="0")) is None
        # a gap in the sequence is tolerated (lost chunks must not deafen STT)
        assert isinstance(await serializer.deserialize(self._media(pcm64, chunk="5")),
                          InputAudioRawFrame)
        # non-numeric chunk ids fall back to always-accept
        assert isinstance(await serializer.deserialize(self._media(pcm64, chunk="x9")),
                          InputAudioRawFrame)

    async def test_malformed_json_base64_and_oversized_payloads_ignored(self):
        serializer = VaaniFrameSerializer(stream_sid="MZ123")
        assert await serializer.deserialize("{not json") is None
        assert await serializer.deserialize(json.dumps(["not", "a", "dict"])) is None
        assert await serializer.deserialize(self._media("!!!not-base64!!!")) is None
        assert await serializer.deserialize(self._media("")) is None
        oversized = "A" * 140_001  # > 100 KB PCM once decoded
        assert await serializer.deserialize(self._media(oversized)) is None

    async def test_unsupported_control_events_are_ignored(self):
        serializer = VaaniFrameSerializer(stream_sid="MZ123")
        events = (
            {"event": "dtmf", "dtmf": {"digit": "5", "duration": 120}},
            {"event": "mark", "mark": {"name": "played-1"}},
            {"event": "marker", "marker": {"name": "played-2"}},
            {"event": "clear", "clear": {"reason": "dialer_request"}},
            {"event": "transfer", "transfer": {"reason": "dialer_request"}},
            {"event": "error", "error": {"code": "dialer_error"}},
            {"event": "hangup", "hangup": {"reason": "normal"}},
        )
        for event in events:
            event["streamSid"] = "MZ123"
            assert await serializer.deserialize(json.dumps(event)) is None

    async def test_stop_event_ends_the_worker_and_duplicates_are_safe(self):
        from pipecat.frames.frames import EndWorkerFrame

        serializer = VaaniFrameSerializer(stream_sid="MZ123")
        stop = json.dumps({"event": "stop", "streamSid": "MZ123",
                           "stop": {"reason": "stop"}})
        frame = await serializer.deserialize(stop)
        # EndWorkerFrame (not EndFrame): only it actually stops the pipeline
        # worker when injected from the input transport.
        assert isinstance(frame, EndWorkerFrame)
        assert frame.reason == "caller_stop"
        assert isinstance(await serializer.deserialize(stop), EndWorkerFrame)

    async def test_end_frame_emits_stop_exactly_once_even_with_tail_audio(self):
        from pipecat.frames.frames import EndFrame

        serializer = VaaniFrameSerializer(stream_sid="MZ123")
        # a sub-chunk audio tail is pending when the call ends
        assert await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 640, sample_rate=8000, num_channels=1,
        )) is None
        message = json.loads(await serializer.serialize(EndFrame()))
        assert message == {"event": "stop", "streamSid": "MZ123",
                           "stop": {"reason": "stop"}}
        # nothing (audio, clear, second stop) may follow the stop event
        assert await serializer.serialize(EndFrame()) is None
        assert await serializer.serialize(InterruptionFrame()) is None
        assert await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 3200, sample_rate=8000, num_channels=1,
        )) is None

    async def test_transfer_carries_queue_and_agent(self):
        serializer = VaaniFrameSerializer(stream_sid="MZ123")
        raw = await serializer.serialize(OutputTransportMessageFrame(message={
            "type": "telephony_control", "event": "transfer",
            "reason": "workflow_handover", "transfer_queue": "queue 1",
            "agent_id": "agent 1",
        }))
        assert json.loads(raw) == {
            "event": "transfer", "streamSid": "MZ123",
            "transfer": {"reason": "workflow_handover",
                         "transfer_queue": "queue 1", "agent_id": "agent 1"},
        }
