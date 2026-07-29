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
from voice_runtime.telephony import (
    FreeSwitchAudioStreamSerializer,
    VaaniFrameSerializer,
    build_media_serializer,
)

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


def _pcm_tone(samples: int, amplitude: int, *, period: int = 16) -> bytes:
    """Square-ish test tone: alternating ±amplitude every half period."""
    import struct

    out = bytearray()
    for index in range(samples):
        value = amplitude if (index // (period // 2)) % 2 == 0 else -amplitude
        out += struct.pack("<h", value)
    return bytes(out)


def _interleave(first: bytes, second: bytes) -> bytes:
    return b"".join(
        first[index:index + 2] + second[index:index + 2]
        for index in range(0, len(first), 2)
    )


_SILENCE_160 = b"\x00\x00" * 160


class TestFreeSwitchTelephony:
    def test_connect_payload_supports_audio_stream_and_legacy_fork_key(self):
        instructions = connect_instructions(
            "freeswitch",
            TelephonyProviderConfig(
                provider="freeswitch", public_ws_base="ws://voice.example.com"
            ),
            "vs_456",
        )
        url = "ws://voice.example.com/ws/telephony/freeswitch/vs_456"
        assert json.loads(instructions.body) == {
            "audio_stream_url": url,
            "audio_fork_url": url,
        }

    def test_public_websocket_base_is_required(self):
        with pytest.raises(ApiError, match="public_ws_base"):
            connect_instructions(
                "freeswitch",
                TelephonyProviderConfig(provider="freeswitch"),
                "vs_456",
            )

    def test_factory_selects_mod_audio_stream_serializer(self):
        assert isinstance(
            build_media_serializer("freeswitch"),
            FreeSwitchAudioStreamSerializer,
        )

    async def test_binary_caller_audio_uses_first_stream_little_endian(self):
        # Capture analysis 2026-07-29: BOTH streams are little-endian (real
        # caller speech: adjacent-sample corr 0.87 LE vs 0.11 byte-swapped;
        # known-good TTS write stream: 0.92 LE vs 0.05 swapped). No byteswap.
        # input_gain=1 isolates the wire format from level handling.
        serializer = FreeSwitchAudioStreamSerializer(input_gain=1.0)
        caller_pcm = b"\x01\x02" * 160
        bot_pcm = b"\x03\x04" * 160
        frame = await serializer.deserialize(_interleave(caller_pcm, bot_pcm))
        assert isinstance(frame, InputAudioRawFrame)
        assert frame.audio == caller_pcm
        assert frame.sample_rate == 8000
        assert frame.num_channels == 1

    async def test_pinned_second_channel_is_used_immediately(self):
        serializer = FreeSwitchAudioStreamSerializer(
            caller_channel="second", input_gain=1.0
        )
        caller_pcm = b"\x05\x06" * 160
        bot_pcm = b"\x01\x02" * 160
        frame = await serializer.deserialize(_interleave(bot_pcm, caller_pcm))
        assert frame.audio == caller_pcm

    async def test_quiet_caller_speech_is_gained_for_vad(self):
        # Live calls: caller speech at ~0.18 full-scale sits just under the
        # telephony VAD volume gate. The base gain applies until a level is
        # observed, so the very first quiet utterance already clears VAD.
        serializer = FreeSwitchAudioStreamSerializer(input_gain=12.0)
        caller = _pcm_tone(160, 800)
        frame = await serializer.deserialize(_interleave(caller, _SILENCE_160))
        samples = frame.audio
        assert max(
            abs(int.from_bytes(samples[i:i + 2], "little", signed=True))
            for i in range(0, len(samples), 2)
        ) == 9600  # 800 × 12

    async def test_adaptive_gain_never_clips_loud_speech(self):
        serializer = FreeSwitchAudioStreamSerializer(input_gain=12.0)
        loud = _pcm_tone(160, 8000)
        frame = None
        for _ in range(30):  # enough voiced evidence to track the level
            frame = await serializer.deserialize(_interleave(loud, _SILENCE_160))
        samples = frame.audio
        peak = max(
            abs(int.from_bytes(samples[i:i + 2], "little", signed=True))
            for i in range(0, len(samples), 2)
        )
        assert peak == 16000  # gained to the -6 dBFS target, not 8000 × 12

    async def test_playback_echo_is_not_gained(self):
        # Right after bot audio goes out, sub-echo-gate levels must pass
        # UNGAINED so the greeting's line echo cannot trip VAD/barge-in.
        serializer = FreeSwitchAudioStreamSerializer(input_gain=12.0)
        await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 3200, sample_rate=8000, num_channels=1,
        ))
        echo = _pcm_tone(160, 800)
        frame = await serializer.deserialize(_interleave(echo, _SILENCE_160))
        assert frame.audio == echo  # passthrough, no ×12

    async def test_own_playback_on_selected_channel_is_muted_then_fled(self):
        # If the selected (unlocked) stream turns out to carry the bot's own
        # TTS, those messages are muted (never fed to VAD/STT) and the
        # serializer switches to the other stream — the 2026-07-29 live
        # self-barge-in during the greeting can never happen again.
        serializer = FreeSwitchAudioStreamSerializer(input_gain=12.0)
        await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 3200, sample_rate=8000, num_channels=1,
        ))
        tts_leak = _pcm_tone(160, 12000)
        for _ in range(14):
            frame = await serializer.deserialize(
                _interleave(tts_leak, _SILENCE_160)
            )
            assert frame.audio == _SILENCE_160  # muted, selection unchanged
        assert serializer._selected == 0
        for _ in range(2):
            await serializer.deserialize(_interleave(tts_leak, _SILENCE_160))
        assert serializer._selected == 1  # fled the playback-carrying stream
        assert serializer._muted_msgs >= 14

    async def test_auto_switches_to_voiced_stream_when_selected_is_silent(self):
        serializer = FreeSwitchAudioStreamSerializer(input_gain=1.0)
        voice = _pcm_tone(160, 1000)
        frame = None
        for _ in range(30):  # bot is quiet the whole time
            frame = await serializer.deserialize(_interleave(_SILENCE_160, voice))
        assert serializer._selected == 1
        assert frame.audio == voice

    async def test_mono_at_double_rate_is_detected_and_passed_through(self):
        import math as _math
        import struct as _struct

        serializer = FreeSwitchAudioStreamSerializer(input_gain=1.0)
        mono_16k = b"".join(
            _struct.pack("<h", int(9000 * _math.sin(2 * _math.pi * 400 * i / 16000)))
            for i in range(320)
        )
        frame = None
        for _ in range(30):
            frame = await serializer.deserialize(mono_16k)
        assert serializer._mono_2x is True
        assert frame.sample_rate == 16000
        assert frame.audio == mono_16k

    async def test_debug_capture_writes_both_streams(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ECHOSPHERE_FS_AUDIO_DEBUG_DIR", str(tmp_path))
        serializer = FreeSwitchAudioStreamSerializer(input_gain=1.0)
        msg = _interleave(_pcm_tone(160, 700), _SILENCE_160)
        await serializer.deserialize(msg)
        await serializer.deserialize(msg)
        firsts = list(tmp_path.glob("echosphere-fs-*-first.s16le"))
        seconds = list(tmp_path.glob("echosphere-fs-*-second.s16le"))
        assert len(firsts) == 1 and len(seconds) == 1
        assert serializer._debug_audio_remaining == 8000 * 2 * 20 - 640

    async def test_bot_audio_uses_mod_audio_stream_playback_envelope(self):
        serializer = FreeSwitchAudioStreamSerializer()
        pcm = b"\x03\x04" * 160
        assert await serializer.serialize(OutputAudioRawFrame(
            audio=pcm, sample_rate=8000, num_channels=1,
        )) is None
        raw = await serializer.serialize(BotStoppedSpeakingFrame())
        assert json.loads(raw) == {
            "type": "streamAudio",
            "data": {
                "audioDataType": "raw",
                "sampleRate": 8000,
                "audioData": base64.b64encode(pcm).decode("ascii"),
            },
        }

    async def test_text_metadata_is_not_treated_as_audio(self):
        serializer = FreeSwitchAudioStreamSerializer()
        assert await serializer.deserialize('{"event":"connected"}') is None

    async def test_audio_is_batched_before_module_file_playback(self):
        serializer = FreeSwitchAudioStreamSerializer()
        assert await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 1600, sample_rate=8000, num_channels=1,
        )) is None
        raw = await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x02" * 1600, sample_rate=8000, num_channels=1,
        ))
        audio = base64.b64decode(json.loads(raw)["data"]["audioData"])
        assert len(audio) == 3200
        assert audio == b"\x01" * 1600 + b"\x02" * 1600
