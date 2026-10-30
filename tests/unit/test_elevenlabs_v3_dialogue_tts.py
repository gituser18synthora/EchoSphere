"""ElevenLabs Eleven v3 dialogue adapter against a scriptable mock server.

Every test here pins a behaviour that was first verified against the live
ElevenLabs Text-to-Dialogue endpoint (2026-09-22) and would otherwise be a
silent call-killer:

- ``close_context`` flushes rather than cancels, so a graceful end-of-reply
  must KEEP the tail (the reply's last words) while a barge-in must DROP it;
- one stray message to a closing context closes the whole socket, so sends
  are gated and retired generations can never re-open a context;
- a closing context still occupies one of the five slots until its
  ``is_final`` arrives;
- ``is_final_audio_for_turn`` is a turn boundary, NOT reply completion — the
  router must get exactly one ``final`` per reply.
"""

import asyncio
import json

import pytest

import shared.providers.tts.elevenlabs_v3_ws as v3_ws
from shared.providers.base import ProviderError
from shared.providers.tts.elevenlabs_v3_ws import ElevenLabsV3DialogueTTSProvider
from shared.providers.tts.streaming import TTSStreamSettings
from tests.mock_tts_servers import (
    API_KEY,
    PCM_CHUNK,
    MockElevenLabsDialogueServer,
)

MODEL = "eleven_v3_conversational"
VOICE = "f1abxvIEijusskcPWE5x"


def settings(**overrides) -> TTSStreamSettings:
    values = dict(
        provider="elevenlabs", model=MODEL, voice=VOICE, language="hi-IN",
        sample_rate=8000, codec="pcm", params={"stability": 0.5},
        api_key=API_KEY, timeout_seconds=3.0,
    )
    values.update(overrides)
    return TTSStreamSettings(**values)


async def drain(provider, *, timeout=5.0):
    """Collect events until every live generation has finalized or errored."""
    audio: dict[str, list[bytes]] = {}
    finals: list[str] = []
    errors: list[ProviderError] = []
    try:
        async with asyncio.timeout(timeout):
            while True:
                event = await provider.events.get()
                if event.kind == "audio":
                    audio.setdefault(event.generation_id, []).append(event.audio)
                elif event.kind == "final":
                    finals.append(event.generation_id)
                    break
                elif event.kind == "error":
                    errors.append(event.error)
                    break
    except TimeoutError:
        pass
    return audio, finals, errors


async def collect(provider, count, *, timeout=5.0):
    """Collect exactly `count` events (any kind), or fewer on timeout."""
    events = []
    try:
        async with asyncio.timeout(timeout):
            for _ in range(count):
                events.append(await provider.events.get())
    except TimeoutError:
        pass
    return events


@pytest.fixture
def base(monkeypatch):
    def _patch(server):
        monkeypatch.setattr(v3_ws, "_WS_BASE", server.url)
    return _patch


class TestWireProtocol:
    async def test_url_carries_model_and_output_format_not_the_voice(self, base):
        """The voice is a per-context field here, unlike the TTS socket where
        it is in the path — so a voice change must not force a reconnect."""
        async with MockElevenLabsDialogueServer() as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(settings())
            await provider.connect()
            await provider.synthesize_stream("नमस्ते", generation_id="g1")
            await provider.flush("g1")
            await drain(provider)
            await provider.close()
            path = server.paths[0]
            assert "/v1/text-to-dialogue/multi-stream-input" in path
            assert f"model_id={MODEL}" in path
            assert "output_format=pcm_8000" in path
            assert VOICE not in path
            # No language_code: the dialogue models take none (see adapter).
            assert "language_code" not in path

    async def test_telephony_codec_maps_to_ulaw(self, base):
        async with MockElevenLabsDialogueServer() as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(
                settings(codec="mulaw", sample_rate=8000))
            await provider.connect()
            await provider.close()
            assert "output_format=ulaw_8000" in server.paths[0]

    async def test_context_init_sends_voices_and_only_stability(self, base):
        """similarity_boost/style/speed belong to other models; sending them
        to the dialogue endpoint is not supported, so they are dropped."""
        async with MockElevenLabsDialogueServer() as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(settings(params={
                "stability": 1.0, "similarity_boost": 0.9, "style": 0.4,
                "speed": 1.1, "use_speaker_boost": True,
            }))
            await provider.connect()
            await provider.synthesize_stream("नमस्ते", generation_id="g1")
            await provider.flush("g1")
            await drain(provider)
            await provider.close()
            assert server.inits[0]["voices"] == [VOICE]
            assert server.inits[0]["voice_settings"] == {"stability": 1.0}

    async def test_text_is_sent_as_inputs_with_new_turn_false(self, base):
        """A reply is ONE turn. new_turn per sentence makes the server emit a
        turn-final per sentence and re-plan prosody mid-reply."""
        async with MockElevenLabsDialogueServer() as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(settings())
            await provider.connect()
            for text in ("पहला वाक्य।", "दूसरा वाक्य।", "तीसरा वाक्य।"):
                await provider.synthesize_stream(text, generation_id="g1")
            await provider.flush("g1")
            await provider.finish("g1")
            audio, finals, errors = await drain(provider)
            await provider.close()
            assert server.texts() == ["पहला वाक्य।", "दूसरा वाक्य।", "तीसरा वाक्य।"]
            assert server.turns() == [False, False, False]
            assert errors == []


class TestReplyCompletion:
    async def test_turn_final_is_not_reply_completion(self, base):
        """is_final_audio_for_turn must never surface as `final`, or the
        router finalizes the generation while text is still streaming."""
        async with MockElevenLabsDialogueServer() as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(settings())
            await provider.connect()
            await provider.synthesize_stream("पहला वाक्य।", generation_id="g1")
            await provider.flush("g1")          # -> audio + turn-final only
            events = await collect(provider, 4, timeout=2.0)
            assert [e.kind for e in events] == ["audio"] * 3
            assert not any(e.kind == "final" for e in events)
            # ... and the generation is still open for more text.
            await provider.synthesize_stream("दूसरा वाक्य।", generation_id="g1")
            await provider.finish("g1")
            audio, finals, errors = await drain(provider)
            await provider.close()
            assert finals == ["g1"]
            assert errors == []
            assert server.texts() == ["पहला वाक्य।", "दूसरा वाक्य।"]

    async def test_exactly_one_final_per_reply(self, base):
        async with MockElevenLabsDialogueServer() as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(settings())
            await provider.connect()
            await provider.synthesize_stream("एक।", generation_id="g1")
            await provider.flush("g1")
            await provider.synthesize_stream("दो।", generation_id="g1")
            await provider.flush("g1")
            await provider.finish("g1")
            finals = 0
            try:
                async with asyncio.timeout(3.0):
                    while True:
                        event = await provider.events.get()
                        if event.kind == "final":
                            finals += 1
            except TimeoutError:
                pass
            await provider.close()
            assert finals == 1

    async def test_graceful_finish_keeps_the_flushed_tail(self, base):
        """close_context flushes: those trailing bytes are the reply's last
        words and must reach the caller."""
        async with MockElevenLabsDialogueServer(chunks=2, tail_chunks=3) as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(settings())
            await provider.connect()
            await provider.synthesize_stream("पूरा जवाब।", generation_id="g1")
            await provider.flush("g1")
            await provider.finish("g1")
            audio, finals, errors = await drain(provider)
            await provider.close()
            assert finals == ["g1"]
            # 2 from the flush + 3 from the close-flush — none dropped.
            assert len(audio["g1"]) == 5
            assert b"".join(audio["g1"]) == PCM_CHUNK * 5


class TestCancellation:
    async def test_cancel_drops_the_flushed_tail(self, base):
        async with MockElevenLabsDialogueServer(chunks=2, tail_chunks=4) as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(settings())
            await provider.connect()
            await provider.synthesize_stream("लंबा जवाब।", generation_id="g1")
            await provider.flush("g1")
            events = await collect(provider, 2, timeout=2.0)
            assert [e.kind for e in events] == ["audio", "audio"]
            await provider.cancel("g1")
            # The server flushes 4 more chunks; none may be emitted, and no
            # `final` may be reported for a cancelled generation.
            leftover = await collect(provider, 1, timeout=1.5)
            await provider.close()
            assert leftover == []
            assert server.closed_contexts  # the context WAS closed server-side

    async def test_late_text_never_reopens_a_retired_context(self, base):
        """The killer race: a sentence dispatched after a barge-in. Re-opening
        the context would answer `context_closing` and close the socket."""
        async with MockElevenLabsDialogueServer() as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(settings())
            await provider.connect()
            await provider.synthesize_stream("पहला।", generation_id="g1")
            await provider.cancel("g1")
            # Late sentences for the cancelled generation.
            await provider.synthesize_stream("देर से आया।", generation_id="g1")
            await provider.flush("g1")
            await provider.finish("g1")
            await asyncio.sleep(0.3)
            # The next reply still works on the SAME socket.
            await provider.synthesize_stream("अगला जवाब।", generation_id="g2")
            await provider.flush("g2")
            await provider.finish("g2")
            audio, finals, errors = await drain(provider)
            await provider.close()
            assert server.violations == []      # never messaged a closing ctx
            assert server.connections == 1      # no reconnect was needed
            assert "देर से आया।" not in server.texts()
            assert finals == ["g2"]
            assert errors == []
            assert audio.get("g2")

    async def test_next_reply_reuses_the_socket_after_barge_in(self, base):
        async with MockElevenLabsDialogueServer(chunks=2, tail_chunks=3) as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(settings())
            await provider.connect()
            await provider.synthesize_stream("रोका गया।", generation_id="g1")
            await provider.flush("g1")
            # Consume BOTH pre-barge-in chunks: audio already delivered is
            # legitimate, and leaving it queued would confuse it with a leak.
            before = await collect(provider, 2, timeout=2.0)
            assert [e.kind for e in before] == ["audio", "audio"]
            await provider.cancel("g1")
            await provider.synthesize_stream("नया जवाब।", generation_id="g2")
            await provider.flush("g2")
            await provider.finish("g2")
            audio, finals, errors = await drain(provider)
            await provider.close()
            assert server.connections == 1
            assert finals == ["g2"]
            # The 3 tail chunks the server flushed for g1 after close_context
            # never reached the consumer.
            assert audio.get("g1", []) == []
            assert audio.get("g2")

    async def test_barge_in_racing_a_send_cannot_reach_a_closing_context(
            self, base, monkeypatch):
        """The window that kills the socket: a sentence whose state check has
        passed but whose write has not happened yet, while a barge-in lands.

        The interleaving is forced rather than raced. The first lock
        acquisition is held at the gate; the barge-in then completes entirely
        inside that window. Reading the context state BEFORE taking the lock
        (the natural, wrong implementation) lets the pending sentence write
        after close_context and the server kills the socket — so this test
        fails unless the check and the write are one critical section.
        """
        class GatedLock:
            """Delegates to the real lock, stalling the FIRST acquisition."""

            def __init__(self, inner):
                self._inner = inner
                self.gate = asyncio.Event()
                self.reached = asyncio.Event()
                self._gated = True

            async def __aenter__(self):
                if self._gated:
                    self._gated = False
                    self.reached.set()
                    await self.gate.wait()
                return await self._inner.__aenter__()

            async def __aexit__(self, *exc):
                return await self._inner.__aexit__(*exc)

        async with MockElevenLabsDialogueServer(chunks=1, tail_chunks=1) as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(settings())
            await provider.connect()
            await provider.synthesize_stream("पहला।", generation_id="g1")

            gated = GatedLock(provider._send_lock)
            provider._send_lock = gated

            # The sentence enters the send path and stalls at the lock.
            pending = asyncio.create_task(
                provider.synthesize_stream("दूसरा।", generation_id="g1"))
            await asyncio.wait_for(gated.reached.wait(), timeout=2.0)

            # The barge-in runs to completion inside that window.
            await provider.cancel("g1")

            gated.gate.set()
            await asyncio.wait_for(pending, timeout=2.0)
            await asyncio.sleep(0.3)
            await provider.close()

            assert server.violations == [], "a message reached a closing context"
            assert "दूसरा।" not in server.texts(), (
                "a sentence was written after close_context")

    async def test_keepalive_never_targets_a_closing_context(self, base, monkeypatch):
        monkeypatch.setattr(v3_ws, "_KEEPALIVE_SECONDS", 0.05)
        async with MockElevenLabsDialogueServer(tail_chunks=0) as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(settings())
            await provider.connect()
            await provider.synthesize_stream("रुको।", generation_id="g1")
            await provider.cancel("g1")
            await asyncio.sleep(0.4)            # several keepalive ticks
            await provider.close()
            assert server.violations == []
            assert server.keep_alives == []


class TestContextLimit:
    async def test_closing_contexts_are_released_on_is_final(self, base):
        """Six sequential barge-ins must not exhaust the five slots."""
        async with MockElevenLabsDialogueServer(chunks=1, tail_chunks=1) as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(settings())
            await provider.connect()
            for index in range(6):
                gen = f"g{index}"
                await provider.synthesize_stream("वाक्य।", generation_id=gen)
                await provider.flush(gen)
                await collect(provider, 1, timeout=2.0)
                await provider.cancel(gen)
                await asyncio.sleep(0.15)       # let is_final land
            await provider.synthesize_stream("आखिरी।", generation_id="last")
            await provider.flush("last")
            await provider.finish("last")
            audio, finals, errors = await drain(provider)
            await provider.close()
            assert server.too_many == []
            assert server.violations == []
            assert finals == ["last"]
            assert errors == []

    async def test_sixth_simultaneous_context_waits_instead_of_erroring(self, base):
        """The adapter must not hand the server a 6th context: it waits for a
        slot and surfaces a transient timeout if none frees up."""
        async with MockElevenLabsDialogueServer(chunks=0, tail_chunks=0) as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(settings())
            await provider.connect()
            for index in range(5):
                await provider.synthesize_stream("x", generation_id=f"g{index}")
            assert provider._occupied_slots() == 5
            with pytest.raises(ProviderError) as excinfo:
                await provider.synthesize_stream("overflow", generation_id="g6")
            await provider.close()
            assert excinfo.value.category == "timeout"
            assert server.too_many == []        # never reached the server


class TestFailureHandling:
    async def test_error_frame_is_reported_and_socket_recovers(self, base):
        """Any error frame closes the connection server-side, so the next
        generation has to reconnect rather than write to a dead socket."""
        async with MockElevenLabsDialogueServer(behavior="error_frame") as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(settings())
            await provider.connect()
            await provider.synthesize_stream("नमस्ते", generation_id="g1")
            await provider.flush("g1")
            audio, finals, errors = await drain(provider)
            assert errors and errors[0].category in ("invalid_input", "rate_limit",
                                                     "upstream")
            await provider.close()
            assert server.connections == 1

        async with MockElevenLabsDialogueServer() as healthy:
            base(healthy)
            provider = ElevenLabsV3DialogueTTSProvider(settings())
            await provider.connect()
            await provider.synthesize_stream("फिर से", generation_id="g2")
            await provider.flush("g2")
            await provider.finish("g2")
            audio, finals, errors = await drain(provider)
            await provider.close()
            assert finals == ["g2"] and errors == []

    async def test_idle_disconnect_reconnects_on_next_reply(self, base):
        """Server dropped the socket while idle: the next reply reconnects and
        starts a clean context instead of addressing a dead one."""
        async with MockElevenLabsDialogueServer() as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(settings())
            await provider.connect()
            await provider.synthesize_stream("पहला।", generation_id="g1")
            await provider.flush("g1")
            await provider.finish("g1")
            await drain(provider)
            # Simulate the idle drop the 20s inactivity timeout produces.
            await provider._teardown_socket()
            await provider.synthesize_stream("दूसरा।", generation_id="g2")
            await provider.flush("g2")
            await provider.finish("g2")
            audio, finals, errors = await drain(provider)
            await provider.close()
            assert server.connections == 2
            assert finals == ["g2"] and errors == []
            assert audio.get("g2")

    async def test_zero_audio_final_is_an_error_not_silence(self, base):
        async with MockElevenLabsDialogueServer(behavior="silent",
                                                tail_chunks=0) as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(settings())
            await provider.connect()
            await provider.synthesize_stream("नमस्ते", generation_id="g1")
            await provider.flush("g1")
            await provider.finish("g1")
            audio, finals, errors = await drain(provider)
            await provider.close()
            assert finals == []
            assert errors and errors[0].category == "upstream"

    async def test_auth_failure_surfaces_without_retrying(self, base):
        async with MockElevenLabsDialogueServer(behavior="auth_fail") as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(settings())
            with pytest.raises(ProviderError) as excinfo:
                await provider.connect()
            await provider.close()
            assert excinfo.value.category == "auth"

    async def test_a_flash_model_is_rejected_by_this_adapter(self, base):
        async with MockElevenLabsDialogueServer() as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(
                settings(model="eleven_flash_v2_5"))
            with pytest.raises(ProviderError) as excinfo:
                await provider.connect()
            await provider.close()
            assert excinfo.value.category == "invalid_input"
            assert "Text-to-Dialogue" in str(excinfo.value)
            assert server.connections == 0


class TestConfigure:
    async def test_voice_change_does_not_reconnect(self, base):
        async with MockElevenLabsDialogueServer() as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(settings())
            await provider.connect()
            await provider.configure(settings(voice="another-voice-id"))
            await provider.synthesize_stream("नमस्ते", generation_id="g1")
            await provider.flush("g1")
            await provider.finish("g1")
            await drain(provider)
            await provider.close()
            assert server.connections == 1
            assert server.inits[0]["voices"] == ["another-voice-id"]

    async def test_rate_change_reconnects(self, base):
        async with MockElevenLabsDialogueServer() as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(settings())
            await provider.connect()
            await provider.configure(settings(sample_rate=24000))
            await provider.synthesize_stream("नमस्ते", generation_id="g1")
            await provider.flush("g1")
            await provider.finish("g1")
            await drain(provider)
            await provider.close()
            assert server.connections == 2
            assert "output_format=pcm_24000" in server.paths[-1]


class TestNativeBreathing:
    """Breathing generated by ElevenLabs inside its own speech.

    The tag is injected in the adapter, after every sanitizer, so these tests
    pin the properties that make that safe: once per reply, never on
    continuations, never when disabled, and never for a model that cannot
    parse it.
    """

    def _settings(self, **params):
        base = {"stability": 0.5}
        base.update(params)
        return settings(params=base)

    async def test_tag_is_sent_once_at_the_start_of_a_reply(self, base):
        async with MockElevenLabsDialogueServer() as server:
            provider = ElevenLabsV3DialogueTTSProvider(
                self._settings(native_breathing=True, native_breath_probability=1.0))
            base(server)
            provider._breath_random = __import__("random").Random(1)
            await provider.connect()
            for part in ("पहला वाक्य। ", "दूसरा वाक्य। ", "तीसरा वाक्य।"):
                await provider.synthesize_stream(part, generation_id="g1")
            await provider.flush("g1")
            await provider.finish("g1")
            await drain(provider)
            await provider.close()
            texts = server.texts()
            assert texts[0].startswith("[exhales] "), texts[0]
            # Continuations carry the sentence only — one breath per reply.
            assert all("[exhales]" not in t for t in texts[1:]), texts
            # The spoken words are untouched.
            assert texts[0].endswith("पहला वाक्य। ")

    async def test_no_tag_when_breathing_is_off(self, base):
        async with MockElevenLabsDialogueServer() as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(self._settings())
            await provider.connect()
            await provider.synthesize_stream("नमस्ते।", generation_id="g1")
            await provider.flush("g1"); await provider.finish("g1")
            await drain(provider)
            await provider.close()
            assert server.texts() == ["नमस्ते।"]

    async def test_probability_zero_never_breathes(self, base):
        async with MockElevenLabsDialogueServer() as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(
                self._settings(native_breathing=True, native_breath_probability=0.0))
            await provider.connect()
            for i in range(6):
                await provider.synthesize_stream("नमस्ते।", generation_id=f"g{i}")
                await provider.finish(f"g{i}")
            await asyncio.sleep(0.4)
            await provider.close()
            assert all("[exhales]" not in t for t in server.texts())

    async def test_each_reply_decides_independently(self, base):
        """Occasional means per reply — a new context gets a fresh decision."""
        async with MockElevenLabsDialogueServer() as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(
                self._settings(native_breathing=True, native_breath_probability=1.0))
            await provider.connect()
            for i in range(3):
                await provider.synthesize_stream("नमस्ते।", generation_id=f"g{i}")
                await provider.flush(f"g{i}")
                await provider.finish(f"g{i}")
                await asyncio.sleep(0.2)
            await provider.close()
            tagged = [t for t in server.texts() if t.startswith("[exhales] ")]
            assert len(tagged) == 3, server.texts()

    async def test_suppress_next_breath_skips_exactly_one_reply(self, base):
        async with MockElevenLabsDialogueServer() as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(
                self._settings(native_breathing=True, native_breath_probability=1.0))
            await provider.connect()
            provider.suppress_next_breath()
            await provider.synthesize_stream("पहला।", generation_id="g1")
            await provider.flush("g1"); await provider.finish("g1")
            await asyncio.sleep(0.3)
            await provider.synthesize_stream("दूसरा।", generation_id="g2")
            await provider.flush("g2"); await provider.finish("g2")
            await asyncio.sleep(0.3)
            await provider.close()
            texts = server.texts()
            assert texts[0] == "पहला।"                     # suppressed
            assert texts[1].startswith("[exhales] ")        # back to normal

    async def test_cancelled_reply_does_not_carry_the_tag_forward(self, base):
        """A barge-in retires the context; the next reply still decides
        cleanly rather than inheriting a half-used flag."""
        async with MockElevenLabsDialogueServer() as server:
            base(server)
            provider = ElevenLabsV3DialogueTTSProvider(
                self._settings(native_breathing=True, native_breath_probability=1.0))
            await provider.connect()
            await provider.synthesize_stream("पहला।", generation_id="g1")
            await provider.cancel("g1")
            await provider.synthesize_stream("दूसरा।", generation_id="g2")
            await provider.flush("g2"); await provider.finish("g2")
            await drain(provider)
            await provider.close()
            assert server.violations == []
            assert any(t.startswith("[exhales] दूसरा।") for t in server.texts())
