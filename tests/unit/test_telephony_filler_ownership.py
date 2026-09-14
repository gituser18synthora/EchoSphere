"""Latency filler packets must never share a pending speech packet."""

import base64
import json

import pytest
from pipecat.frames.frames import (
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
    TTSAudioRawFrame,
)

from voice_runtime.frames import (
    AUDIO_FLUSH_MESSAGE_TYPE,
    FillerAudioOwner,
    FillerAudioRawFrame,
    FillerClearFrame,
)
from voice_runtime.telephony import (
    FreeSwitchAudioForkSerializer,
    FreeSwitchAudioStreamSerializer,
    VaaniFrameSerializer,
    FillerSafeSerializer,
    _RAMP_THRESHOLDS,
)


@pytest.fixture(params=["fork", "stream", "vaani"])
def make_serializer(request):
    if request.param == "fork":
        return FreeSwitchAudioForkSerializer
    if request.param == "stream":
        return FreeSwitchAudioStreamSerializer
    return lambda: VaaniFrameSerializer(stream_sid="this-call")


def pcm(value, samples=160):
    return value.to_bytes(2, "little", signed=True) * samples


def filler(owner, audio=None):
    return FillerAudioRawFrame(
        audio=pcm(7000) if audio is None else audio,
        sample_rate=8000,
        num_channels=1,
        owner=owner,
    )


def audio_from_wire(raw):
    message = json.loads(raw)
    if "media" in message:
        payload = message["media"]["payload"]
    else:
        payload = message["data"].get("audioContent") or message["data"]["audioData"]
    return base64.b64decode(payload)


@pytest.mark.asyncio
async def test_filler_packets_leave_immediately_without_advancing_reply_ramp(make_serializer):
    serializer = make_serializer()
    owner = FillerAudioOwner(turn_id=1)
    for _ in range(6):
        assert audio_from_wire(await serializer.serialize(filler(owner))) == pcm(7000)
        assert serializer._pending_audio == b""
        assert serializer._ramp_step == 0
        assert serializer._last_audio_at == 0

    # A response keeps its fast first-packet boundary, even after a long filler.
    reply = pcm(12000, samples=320)
    assert audio_from_wire(await serializer.serialize(TTSAudioRawFrame(
        audio=reply, sample_rate=8000, num_channels=1,
    ))) == reply


@pytest.mark.asyncio
async def test_cleanup_preserves_unrelated_pending_audio_and_never_sends_global_clear(
    make_serializer,
):
    serializer = make_serializer()
    serializer._ramp_step = len(_RAMP_THRESHOLDS)
    unrelated = pcm(1000, samples=400)
    assert await serializer.serialize(OutputAudioRawFrame(
        audio=unrelated, sample_rate=8000, num_channels=1,
    )) is None
    owner = FillerAudioOwner(turn_id=1)
    assert audio_from_wire(await serializer.serialize(filler(owner))) == pcm(7000)
    assert serializer._pending_audio == unrelated

    owner.cancel()
    assert await serializer.serialize(FillerClearFrame(owner=owner)) is None
    assert serializer._pending_audio == unrelated
    assert serializer._ramp_step == len(_RAMP_THRESHOLDS)
    assert await serializer.serialize(OutputTransportMessageFrame(
        message={"type": AUDIO_FLUSH_MESSAGE_TYPE, "filler_owner": owner.token},
    )) is None
    assert serializer._pending_audio == unrelated
    # Ordinary legacy flushes still release only the valid, unrelated audio.
    flushed = audio_from_wire(await serializer.serialize(OutputTransportMessageFrame(
        message={"type": AUDIO_FLUSH_MESSAGE_TYPE},
    )))
    assert flushed == unrelated + bytes(160)


@pytest.mark.asyncio
async def test_cancelled_filler_cannot_be_prepended_to_response(make_serializer):
    serializer = make_serializer()
    owner = FillerAudioOwner(turn_id=1)
    assert audio_from_wire(await serializer.serialize(filler(owner))) == pcm(7000)
    owner.cancel()
    assert await serializer.serialize(FillerClearFrame(owner=owner)) is None
    assert await serializer.serialize(filler(owner)) is None
    reply = pcm(12000, samples=320)
    assert audio_from_wire(await serializer.serialize(TTSAudioRawFrame(
        audio=reply, sample_rate=8000, num_channels=1,
    ))) == reply
    assert serializer._pending_audio == b""


@pytest.mark.asyncio
async def test_six_turns_have_no_stale_filler_in_next_reply(make_serializer):
    serializer = make_serializer()
    previous = None
    for turn in range(1, 7):
        owner = FillerAudioOwner(turn_id=turn)
        if previous is not None:
            assert await serializer.serialize(filler(previous)) is None
        filler_pcm = pcm(7000 + turn)
        assert audio_from_wire(
            await serializer.serialize(filler(owner, filler_pcm))
        ) == filler_pcm
        owner.cancel()
        assert await serializer.serialize(FillerClearFrame(owner=owner)) is None
        assert await serializer.serialize(filler(owner)) is None
        # Flush speech at each turn end, matching the normal bot-stopped path.
        reply = pcm(12000 + turn, samples=1600)
        assert audio_from_wire(await serializer.serialize(TTSAudioRawFrame(
            audio=reply, sample_rate=8000, num_channels=1,
        ))) == reply
        assert serializer._pending_audio == b""
        previous = owner


@pytest.mark.asyncio
async def test_same_turn_number_in_different_calls_does_not_share_cancellation(
    make_serializer,
):
    call_a, call_b = make_serializer(), make_serializer()
    owner_a, owner_b = FillerAudioOwner(turn_id=1), FillerAudioOwner(turn_id=1)
    assert owner_a.token != owner_b.token
    assert audio_from_wire(await call_a.serialize(filler(owner_a))) == pcm(7000)
    owner_a.cancel()
    assert await call_a.serialize(FillerClearFrame(owner=owner_a)) is None
    assert await call_a.serialize(filler(owner_a)) is None
    assert audio_from_wire(
        await call_b.serialize(filler(owner_b, pcm(5000)))
    ) == pcm(5000)
    assert not owner_b.cancelled


@pytest.mark.asyncio
async def test_clear_for_different_owner_preserves_active_filler_and_pending_tts(
    make_serializer,
):
    serializer = make_serializer()
    cleared, current = FillerAudioOwner(turn_id=1), FillerAudioOwner(turn_id=1)
    pending = pcm(12000)
    assert await serializer.serialize(TTSAudioRawFrame(
        audio=pending, sample_rate=8000, num_channels=1,
    )) is None
    cleared.cancel()
    assert await serializer.serialize(FillerClearFrame(owner=cleared)) is None
    assert serializer._pending_audio == pending
    assert audio_from_wire(await serializer.serialize(filler(current))) == pcm(7000)
    assert serializer._pending_audio == pending
    assert not current.cancelled


@pytest.mark.asyncio
async def test_short_filler_packet_is_padded_without_retaining_remainder(make_serializer):
    serializer = make_serializer()
    owner = FillerAudioOwner(turn_id=1)
    assert audio_from_wire(
        await serializer.serialize(filler(owner, pcm(7000, 40)))
    ) == pcm(7000, 40) + bytes(240)
    assert serializer._pending_audio == b""
    assert serializer._ramp_step == 0


@pytest.mark.asyncio
async def test_empty_or_unowned_filler_is_not_serialized(make_serializer):
    serializer = make_serializer()
    owner = FillerAudioOwner(turn_id=1)
    assert await serializer.serialize(filler(owner, b"")) is None
    assert await serializer.serialize(filler(None)) is None
    assert serializer._pending_audio == b""


async def test_non_native_filler_uses_isolated_native_pcm(make_serializer):
    serializer = make_serializer()
    owner = FillerAudioOwner(turn_id=1)
    frame = FillerAudioRawFrame(
        audio=pcm(7000, 480), sample_rate=24000, num_channels=1, owner=owner,
    )
    assert len(audio_from_wire(await serializer.serialize(frame))) == 320
    assert serializer._pending_audio == b"" and serializer._ramp_step == 0


@pytest.mark.parametrize("provider", ["twilio", "telnyx", "plivo", "exotel"])
async def test_third_party_encoder_preserves_response_resampler_history(provider):
    from pipecat.frames.frames import StartFrame
    if provider == "twilio":
        from pipecat.serializers.twilio import TwilioFrameSerializer
        delegate = TwilioFrameSerializer(stream_sid="local", params=TwilioFrameSerializer.InputParams(auto_hang_up=False))
    elif provider == "telnyx":
        from pipecat.serializers.telnyx import TelnyxFrameSerializer
        delegate = TelnyxFrameSerializer(stream_id="local", outbound_encoding="PCMU", inbound_encoding="PCMU", params=TelnyxFrameSerializer.InputParams(auto_hang_up=False))
    elif provider == "plivo":
        from pipecat.serializers.plivo import PlivoFrameSerializer
        delegate = PlivoFrameSerializer(stream_id="local", params=PlivoFrameSerializer.InputParams(auto_hang_up=False))
    else:
        from pipecat.serializers.exotel import ExotelFrameSerializer
        delegate = ExotelFrameSerializer(stream_sid="local")
    serializer = FillerSafeSerializer(delegate)
    await serializer.setup(StartFrame(audio_in_sample_rate=8000, audio_out_sample_rate=24000))
    original = delegate._output_resampler.resample
    rates = []

    async def trace(audio, in_rate, out_rate):
        rates.append((in_rate, out_rate))
        return await original(audio, in_rate, out_rate)

    delegate._output_resampler.resample = trace
    owner = FillerAudioOwner(turn_id=1)
    frame = FillerAudioRawFrame(
        audio=pcm(7000, 480), sample_rate=24000, num_channels=1, owner=owner,
    )
    assert await serializer.serialize(frame)
    # Equal-rate resampling is passthrough: no filler enters SOXR history.
    assert rates == [(8000, 8000)]
    owner.cancel()
    assert await serializer.serialize(frame) is None
    assert await serializer.serialize(FillerClearFrame(owner)) is None
    reply = TTSAudioRawFrame(audio=pcm(12000, 24000), sample_rate=24000, num_channels=1)
    assert await serializer.serialize(reply)
    assert rates[-1] == (24000, 8000)
