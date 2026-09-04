"""Provider factory/config validation, mock providers, PCM + TTS-text utils."""

import base64
import json
import logging
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
from voice_runtime.frames import AUDIO_FLUSH_MESSAGE_TYPE
from voice_runtime.telephony import (
    _RAMP_THRESHOLDS,
    FreeSwitchAudioForkSerializer,
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

    def test_booking_id_is_spoken_digit_by_digit_in_english(self):
        from shared.audio.text import sanitize_for_tts

        out = sanitize_for_tts(
            "Can I help you with your existing booking ID 601001?"
        )
        assert out == "Can I help you with your existing booking ID 6 0 1 0 0 1?"

    def test_booking_id_is_spoken_digit_by_digit_in_hinglish(self):
        from shared.audio.text import sanitize_for_tts

        out = sanitize_for_tts(
            "Kya main aapki booking 601001 ke baare mein madad karun?"
        )
        assert out == (
            "Kya main aapki booking 6 0 1 0 0 1 ke baare mein madad karun?"
        )

    def test_booking_id_is_spoken_digit_by_digit_in_devanagari(self):
        from shared.audio.text import sanitize_for_tts

        out = sanitize_for_tts("क्या मैं आपकी बुकिंग 601001 के बारे में मदद करूँ?")
        assert "बुकिंग 6 0 1 0 0 1" in out

    def test_unlabelled_numbers_keep_their_natural_pronunciation(self):
        from shared.audio.text import sanitize_for_tts

        text = "The pending amount is 601001 rupees and check-in is on 20 August."
        assert sanitize_for_tts(text) == text


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
        """First packets ramp out fast; the steady state is the 200 ms packet.

        The reply's opening audio must not sit in the buffer — that delay is
        the caller's perceived response time. Later packets return to the
        full 3200-byte (200 ms) size, which is what keeps playout stable.
        """
        serializer = VaaniFrameSerializer(stream_sid="MZ123")
        # Ramp step 0 (640 B): the first 40 ms chunk goes out immediately.
        message = json.loads(await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x00" * 640, sample_rate=8000, num_channels=1,
        )))
        first = base64.b64decode(message["media"]["payload"])
        assert message["event"] == "media"
        assert message["streamSid"] == "MZ123"
        assert message["media"]["track"] == "inbound"
        assert message["media"]["chunk"] == "1"
        assert len(first) == 640
        assert len(first) % 320 == 0
        # Ramp climbs: 640 B is now below the step-1 threshold (1280 B).
        assert await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 640, sample_rate=8000, num_channels=1,
        )) is None
        second = base64.b64decode(json.loads(await serializer.serialize(
            OutputAudioRawFrame(audio=b"\x01" * 640, sample_rate=8000,
                                num_channels=1)
        ))["media"]["payload"])
        assert len(second) == 1280
        # Past the ramp, the full 200 ms packet is the rule again.
        for _ in range(3):
            await serializer.serialize(OutputAudioRawFrame(
                audio=b"\x02" * 1280, sample_rate=8000, num_channels=1,
            ))
        serializer._ramp_step = len(_RAMP_THRESHOLDS)
        serializer._pending_audio.clear()
        assert await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x03" * 1600, sample_rate=8000, num_channels=1,
        )) is None
        steady = base64.b64decode(json.loads(await serializer.serialize(
            OutputAudioRawFrame(audio=b"\x03" * 1600, sample_rate=8000,
                                num_channels=1)
        ))["media"]["payload"])
        assert len(steady) == 3200

    async def test_ramp_resets_per_utterance_so_every_reply_starts_fast(self):
        serializer = VaaniFrameSerializer(stream_sid="MZ123")
        serializer._ramp_step = len(_RAMP_THRESHOLDS)
        # An interruption starts a new utterance: the ramp is armed again.
        await serializer.serialize(InterruptionFrame())
        assert json.loads(await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 640, sample_rate=8000, num_channels=1,
        )))["media"]["payload"]
        # …and so does an idle gap between replies.
        serializer._ramp_step = len(_RAMP_THRESHOLDS)
        serializer._last_audio_at -= 5.0
        assert json.loads(await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 640, sample_rate=8000, num_channels=1,
        )))["media"]["payload"]

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
        # A sub-frame remnant (under one 320 B frame) cannot go out on its
        # own; the end of the utterance must still flush it.
        serializer._ramp_step = len(_RAMP_THRESHOLDS)
        assert await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 160, sample_rate=8000, num_channels=1,
        )) is None
        raw = await serializer.serialize(BotStoppedSpeakingFrame())
        message = json.loads(raw)
        audio = base64.b64decode(message["media"]["payload"])
        assert len(audio) == 320  # zero-padded to the frame quantum
        assert len(audio) % 320 == 0

    async def test_audio_flush_message_sends_the_buffered_tail_now(self):
        # A plain-audio clip (latency filler breath) ends without a
        # BotStoppedSpeakingFrame; its <200 ms tail must not wait in the
        # buffer for the next reply. The filler's flush message releases it.
        serializer = VaaniFrameSerializer(stream_sid="MZ123")
        serializer._ramp_step = len(_RAMP_THRESHOLDS)
        assert json.loads(await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 3200, sample_rate=8000, num_channels=1,
        )))["media"]["payload"]
        assert await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 800, sample_rate=8000, num_channels=1,
        )) is None
        raw = await serializer.serialize(OutputTransportMessageFrame(
            message={"type": AUDIO_FLUSH_MESSAGE_TYPE},
        ))
        audio = base64.b64decode(json.loads(raw)["media"]["payload"])
        assert audio.startswith(b"\x01" * 800) and len(audio) == 960  # padded to 320 B frames
        assert not serializer._pending_audio
        # Nothing buffered: the flush is a no-op, not an empty packet.
        assert await serializer.serialize(OutputTransportMessageFrame(
            message={"type": AUDIO_FLUSH_MESSAGE_TYPE},
        )) is None

    @pytest.mark.parametrize("make, kind, key", [
        (FreeSwitchAudioStreamSerializer, "streamAudio", "audioData"),
        (FreeSwitchAudioForkSerializer, "playAudio", "audioContent"),
    ])
    async def test_freeswitch_audio_flush_message_sends_the_buffered_tail(self, make, kind, key):
        serializer = make()
        serializer._ramp_step = len(_RAMP_THRESHOLDS)
        assert await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 800, sample_rate=8000, num_channels=1,
        )) is None
        raw = await serializer.serialize(OutputTransportMessageFrame(
            message={"type": AUDIO_FLUSH_MESSAGE_TYPE},
        ))
        message = json.loads(raw)
        assert message["type"] == kind
        audio = base64.b64decode(message["data"][key])
        assert audio.startswith(b"\x01" * 800) and len(audio) == 960
        assert not serializer._pending_audio

    @pytest.mark.parametrize("make", [
        lambda: VaaniFrameSerializer(stream_sid="MZ123"),
        FreeSwitchAudioStreamSerializer, FreeSwitchAudioForkSerializer,
    ])
    async def test_stale_remnant_is_dropped_instead_of_replaying_before_the_next_reply(
        self, make
    ):
        # Live FreeSWITCH calls: the breath's last <200 ms sat in the buffer
        # for 1-2 s and then played glued to the front of the reply — heard
        # as the breath happening twice. Audio older than the idle gap is
        # a finished sound; it is retired, never prepended.
        serializer = make()
        serializer._ramp_step = len(_RAMP_THRESHOLDS)
        assert await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 800, sample_rate=8000, num_channels=1,
        )) is None
        serializer._last_audio_at -= 5.0
        raw = await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x02" * 640, sample_rate=8000, num_channels=1,
        ))
        message = json.loads(raw)
        if "media" in message:
            payload = message["media"]["payload"]
        else:
            payload = message["data"].get("audioData") or message["data"]["audioContent"]
        assert base64.b64decode(payload) == b"\x02" * 640  # only the NEW utterance
        assert serializer.stale_audio_dropped == 1

    async def test_remnant_within_the_same_utterance_is_kept(self):
        serializer = VaaniFrameSerializer(stream_sid="MZ123")
        serializer._ramp_step = len(_RAMP_THRESHOLDS)
        assert await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 3000, sample_rate=8000, num_channels=1,
        )) is None
        raw = await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x02" * 640, sample_rate=8000, num_channels=1,
        ))
        audio = base64.b64decode(json.loads(raw)["media"]["payload"])
        assert audio == b"\x01" * 3000 + b"\x02" * 520  # whole 320 B frames, nothing dropped
        assert serializer._pending_audio == b"\x02" * 120
        assert serializer.stale_audio_dropped == 0

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
        serializer._ramp_step = len(_RAMP_THRESHOLDS)
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

    async def test_transfer_carries_queue_and_agent(self, caplog):
        caplog.set_level(logging.INFO, logger="voice_runtime.telephony")
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
        assert "vaani websocket outbound transfer" in caplog.text
        assert raw in caplog.text


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
    def test_connect_payload_offers_stream_and_fork_urls(self):
        # The fork URL carries the explicit transport selector; the plain
        # stream URL stays unchanged for legacy mod_audio_stream helpers.
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
            "audio_fork_url": f"{url}?transport=audio_fork",
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

    def test_factory_selects_mod_audio_fork_serializer(self):
        assert isinstance(
            build_media_serializer("freeswitch", transport="audio_fork"),
            FreeSwitchAudioForkSerializer,
        )

    async def test_audio_fork_passes_native_8k_wire_audio_without_conversion(self):
        serializer = FreeSwitchAudioForkSerializer()
        wire_audio = _pcm_tone(160, 800)

        frame = await serializer.deserialize(wire_audio)

        assert isinstance(frame, InputAudioRawFrame)
        assert frame.audio == wire_audio
        assert frame.sample_rate == 8000
        assert frame.num_channels == 1
        assert serializer._inbound_bytes == len(wire_audio)

    # NOTE: the adaptive input-gain / channel-auto-selection / mono-2x
    # detection behaviors this class once tested were removed when the
    # integration moved to mod_audio_fork (caller-only PCM kept native at
    # 8 kHz through EchoSphere and streaming STT). The mod_audio_stream
    # serializer is a fixed first-channel passthrough.

    async def test_binary_caller_audio_uses_first_stream_little_endian(self):
        # Capture analysis 2026-07-29: BOTH streams are little-endian (real
        # caller speech: adjacent-sample corr 0.87 LE vs 0.11 byte-swapped;
        # known-good TTS write stream: 0.92 LE vs 0.05 swapped). No byteswap.
        serializer = FreeSwitchAudioStreamSerializer()
        caller_pcm = b"\x01\x02" * 160
        bot_pcm = b"\x03\x04" * 160
        frame = await serializer.deserialize(_interleave(caller_pcm, bot_pcm))
        assert isinstance(frame, InputAudioRawFrame)
        assert frame.audio == caller_pcm
        assert frame.sample_rate == 8000
        assert frame.num_channels == 1

    async def test_caller_audio_is_passed_through_ungained(self):
        # No level correction on the stream path: what the wire carries is
        # what VAD/STT sees (the fork transport delivers 16 kHz caller-only
        # audio, which needs no gain).
        serializer = FreeSwitchAudioStreamSerializer()
        await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 3200, sample_rate=8000, num_channels=1,
        ))
        caller = _pcm_tone(160, 800)
        frame = await serializer.deserialize(_interleave(caller, _SILENCE_160))
        assert frame.audio == caller  # passthrough, bit-exact

    async def test_incomplete_trailing_stereo_frame_is_dropped(self):
        # A partial 4-byte frame must not shift channel alignment for every
        # subsequent sample.
        serializer = FreeSwitchAudioStreamSerializer()
        caller_pcm = b"\x01\x02" * 160
        bot_pcm = b"\x03\x04" * 160
        frame = await serializer.deserialize(
            _interleave(caller_pcm, bot_pcm) + b"\x05"
        )
        assert frame.audio == caller_pcm

    async def test_debug_capture_writes_both_streams(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ECHOSPHERE_FS_AUDIO_DEBUG_DIR", str(tmp_path))
        serializer = FreeSwitchAudioStreamSerializer()
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

    async def test_barge_in_clears_pending_audio_and_sends_kill_audio(self):
        # Local pending bytes must never survive an interruption, and the
        # module must be told to drop what was ALREADY shipped (killAudio) —
        # otherwise up to ~2 s of stale bot audio talks over the caller.
        serializer = FreeSwitchAudioStreamSerializer()
        await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 1600, sample_rate=8000, num_channels=1,
        ))
        message = await serializer.serialize(InterruptionFrame())
        assert json.loads(message) == {"type": "killAudio"}
        assert len(serializer._pending_audio) == 0
        # Nothing stale may be flushed after the barge-in.
        assert await serializer.serialize(BotStoppedSpeakingFrame()) is None

    async def test_kill_audio_can_be_disabled_for_older_module_builds(self):
        serializer = FreeSwitchAudioStreamSerializer(send_kill_audio=False)
        await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 1600, sample_rate=8000, num_channels=1,
        ))
        assert await serializer.serialize(InterruptionFrame()) is None
        assert len(serializer._pending_audio) == 0

    async def test_outbound_envelope_always_declares_8k_and_320_byte_frames(self):
        # The wire contract: L16@8k mono, frame-aligned. 3200 bytes at
        # 8000 Hz × 2 bytes/sample × 1 channel = exactly 200 ms of speech —
        # a mislabeled rate here is what wrong-speed playback sounds like.
        serializer = FreeSwitchAudioStreamSerializer()
        serializer._ramp_step = len(_RAMP_THRESHOLDS)  # steady state
        raw = None
        for _ in range(3):  # 3 × 1280 = 3840 bytes ≥ min chunk
            raw = await serializer.serialize(OutputAudioRawFrame(
                audio=b"\x02" * 1280, sample_rate=8000, num_channels=1,
            )) or raw
        payload = json.loads(raw)
        assert payload["data"]["sampleRate"] == 8000
        audio = base64.b64decode(payload["data"]["audioData"])
        assert len(audio) % 320 == 0
        assert len(audio) / (8000 * 2) == pytest.approx(0.2, abs=0.05)

    async def test_audio_is_batched_before_module_file_playback(self):
        """Steady-state batching is unchanged; only the ramp starts small."""
        serializer = FreeSwitchAudioStreamSerializer()
        serializer._ramp_step = len(_RAMP_THRESHOLDS)
        assert await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 1600, sample_rate=8000, num_channels=1,
        )) is None
        raw = await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x02" * 1600, sample_rate=8000, num_channels=1,
        ))
        audio = base64.b64decode(json.loads(raw)["data"]["audioData"])
        assert len(audio) == 3200
        assert audio == b"\x01" * 1600 + b"\x02" * 1600

    async def test_first_reply_audio_is_not_held_for_the_full_batch(self):
        """The opening audio of a reply leaves immediately (latency path)."""
        serializer = FreeSwitchAudioStreamSerializer()
        raw = await serializer.serialize(OutputAudioRawFrame(
            audio=b"\x01" * 640, sample_rate=8000, num_channels=1,
        ))
        audio = base64.b64decode(json.loads(raw)["data"]["audioData"])
        assert len(audio) == 640
        assert len(audio) % 320 == 0
