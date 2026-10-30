"""Eleven v3 Conversational inside the real StreamingTTSRouter pipeline.

These run the actual router against the mock Text-to-Dialogue server, so they
exercise the integration the protocol probes could not: sentence aggregation,
one completion per reply, Pipecat barge-in clearing queued audio, the next
reply on the same socket, idle reconnect — and that the model the operator
selected is the model that reaches ElevenLabs.
"""

import asyncio

import pytest
from pipecat.frames.frames import (
    EndWorkerFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
)

import shared.providers.tts.elevenlabs_v3_ws as v3_ws
from voice_runtime.tts_router import StreamingTTSRouter
from tests.integration.test_tts_router import (
    AudioCollector,
    make_recorder,
    run_router,
    speak_turn,
)
from tests.mock_tts_servers import API_KEY, MockElevenLabsDialogueServer

pytestmark = pytest.mark.integration

MODEL = "eleven_v3_conversational"


def v3_config(ref="env:TEST_EL_V3_KEY", *, language_map=None, fallback=None,
              model=MODEL, native_breathing=None):
    settings = {"stability": 0.5}
    if native_breathing is not None:
        settings["native_breathing"] = native_breathing
    return {
        "provider": "elevenlabs", "model": model, "voice": "monika-wire-id",
        "settings": settings,
        "api_key_reference": ref,
        "language_map": language_map or {},
        "fallback": fallback,
    }


class TestV3DialogueInRouter:
    async def test_multi_sentence_reply_streams_and_completes_once(self, monkeypatch):
        """A three-sentence reply: every sentence reaches the provider in
        order, all of it as ONE turn, and the reply completes exactly once."""
        monkeypatch.setenv("TEST_EL_V3_KEY", API_KEY)
        async with MockElevenLabsDialogueServer(chunks=2, tail_chunks=2) as server:
            monkeypatch.setattr(v3_ws, "_WS_BASE", server.url)
            recorder = make_recorder("vs_v3_multi")
            router = StreamingTTSRouter(
                tts_config=v3_config(), language="hi-IN",
                sample_rate=8000, recorder=recorder,
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, [
                    "पहला वाक्य यहाँ है। ",
                    "दूसरा वाक्य अब आता है। ",
                    "और यह आखिरी वाक्य है।",
                ])
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        texts = server.texts()
        assert len(texts) == 3, texts
        assert texts[0].startswith("पहला")
        assert texts[1].startswith("दूसरा")
        # The reply's FINAL words were sent, not lost to the close.
        assert "आखिरी" in texts[2]
        # One turn for the whole reply — never one per sentence.
        assert server.turns() == [False, False, False]
        assert server.connections == 1
        assert server.violations == []
        assert server.too_many == []
        assert collector.audio_count() > 0
        # Exactly one reply completion reached the transport.
        assert sum(1 for kind, _ in collector.events if kind == "stopped") == 1

    async def test_selected_model_reaches_elevenlabs_unchanged(self, monkeypatch):
        """No silent revert to Flash: the wire URL carries what was picked,
        and the v3 adapter — not the text-to-speech one — is what ran."""
        monkeypatch.setenv("TEST_EL_V3_KEY", API_KEY)
        async with MockElevenLabsDialogueServer(chunks=1) as server:
            monkeypatch.setattr(v3_ws, "_WS_BASE", server.url)
            router = StreamingTTSRouter(
                tts_config=v3_config(), language="hi-IN",
                sample_rate=8000, recorder=make_recorder("vs_v3_model"),
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, ["नमस्ते जी।"])
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        path = server.paths[0]
        assert f"model_id={MODEL}" in path
        assert "eleven_flash" not in path
        assert "/v1/text-to-dialogue/multi-stream-input" in path
        assert "output_format=pcm_8000" in path

    async def test_barge_in_clears_queued_audio_and_next_reply_works(self, monkeypatch):
        """The flushed tail after close_context must not reach the transport,
        and the following reply must run on the SAME socket."""
        monkeypatch.setenv("TEST_EL_V3_KEY", API_KEY)
        async with MockElevenLabsDialogueServer(
                chunks=3, tail_chunks=4, first_chunk_delay=0.4) as server:
            monkeypatch.setattr(v3_ws, "_WS_BASE", server.url)
            router = StreamingTTSRouter(
                tts_config=v3_config(), language="hi-IN",
                sample_rate=8000, recorder=make_recorder("vs_v3_barge"),
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await worker.queue_frame(LLMFullResponseStartFrame())
                await worker.queue_frame(
                    TextFrame("यह एक लंबा जवाब है जिसे बीच में रोका जाएगा। "))
                await worker.queue_frame(LLMFullResponseEndFrame())
                await asyncio.sleep(0.15)          # before the delayed chunk
                await worker.queue_frame(InterruptionFrame())
                await asyncio.sleep(0.8)           # server flushes the tail
                interrupted = collector.audio_count()
                assert interrupted == 0, "cancelled audio reached the transport"
                # Next reply on the same connection.
                await speak_turn(worker, ["ठीक है, बताइए।"])
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        assert server.closed_contexts, "close_context was not sent on barge-in"
        assert server.violations == [], "messaged a closing context"
        assert server.connections == 1, "barge-in forced a reconnect"
        assert collector.audio_count() > 0, "the reply after barge-in was silent"
        assert "ठीक है, बताइए।" in "".join(server.texts())

    async def test_repeated_barge_ins_do_not_exhaust_contexts(self, monkeypatch):
        monkeypatch.setenv("TEST_EL_V3_KEY", API_KEY)
        async with MockElevenLabsDialogueServer(
                chunks=2, tail_chunks=2, first_chunk_delay=0.25) as server:
            monkeypatch.setattr(v3_ws, "_WS_BASE", server.url)
            router = StreamingTTSRouter(
                tts_config=v3_config(), language="hi-IN",
                sample_rate=8000, recorder=make_recorder("vs_v3_rapid"),
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                for index in range(6):
                    await worker.queue_frame(LLMFullResponseStartFrame())
                    await worker.queue_frame(TextFrame(f"जवाब नंबर {index} यहाँ है। "))
                    await worker.queue_frame(LLMFullResponseEndFrame())
                    await asyncio.sleep(0.12)
                    await worker.queue_frame(InterruptionFrame())
                    await asyncio.sleep(0.25)
                await speak_turn(worker, ["आखिरी जवाब पूरा सुनाई देगा।"])
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        assert server.too_many == [], "hit the 5-context limit"
        assert server.violations == []
        assert server.connections == 1
        assert collector.audio_count() > 0
        assert "आखिरी जवाब" in "".join(server.texts())

    async def test_idle_disconnect_recovers_on_the_next_reply(self, monkeypatch):
        """The server drops an idle socket after 20 s; the next reply must
        reconnect rather than write into a dead connection."""
        monkeypatch.setenv("TEST_EL_V3_KEY", API_KEY)
        async with MockElevenLabsDialogueServer(chunks=2, tail_chunks=1) as server:
            monkeypatch.setattr(v3_ws, "_WS_BASE", server.url)
            router = StreamingTTSRouter(
                tts_config=v3_config(), language="hi-IN",
                sample_rate=8000, recorder=make_recorder("vs_v3_idle"),
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, ["पहला जवाब।"])
                first = collector.audio_count()
                assert first > 0
                # Drop every provider socket the router holds, the way an
                # idle-timeout close would.
                for provider in list(router._providers.values()):
                    await provider._teardown_socket()
                await asyncio.sleep(0.2)
                await speak_turn(worker, ["दूसरा जवाब भी सुनाई देगा।"])
                assert collector.audio_count() > first, "no audio after reconnect"
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        assert server.connections == 2, "did not reconnect"
        assert server.violations == []
        assert "दूसरा जवाब" in "".join(server.texts())

    async def test_per_language_override_routes_to_the_v3_adapter(self, monkeypatch):
        """A per-language mapping onto the model must reach the dialogue
        endpoint — this is what a non-streaming model could never do."""
        monkeypatch.setenv("TEST_EL_V3_KEY", API_KEY)
        async with MockElevenLabsDialogueServer(chunks=2) as server:
            monkeypatch.setattr(v3_ws, "_WS_BASE", server.url)
            router = StreamingTTSRouter(
                tts_config=v3_config(language_map={
                    "ml-IN": {
                        "provider": "elevenlabs", "model": MODEL,
                        "voice": "monika-wire-id", "params": {"stability": 1.0},
                        "api_key_reference": "env:TEST_EL_V3_KEY",
                    },
                }),
                language="hi-IN", sample_rate=8000,
                recorder=make_recorder("vs_v3_lang"),
            )
            collector = AudioCollector()

            async def feeder(worker):
                from voice_runtime.frames import SwitchVoiceLanguageFrame
                await asyncio.sleep(0.2)
                await worker.queue_frame(SwitchVoiceLanguageFrame(language="ml-IN"))
                await asyncio.sleep(0.1)
                await speak_turn(worker, ["നമസ്കാരം, എന്ത് സഹായം വേണം?"])
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        assert collector.audio_count() > 0
        assert any(f"model_id={MODEL}" in p for p in server.paths)
        # The override's own stability reached the context init.
        assert server.inits[-1]["voice_settings"] == {"stability": 1.0}


class TestNativeBreathingRouting:
    """The Breathing toggle must pick the RIGHT breathing mechanism per
    effective engine: native tags on v3 Conversational, our clips elsewhere,
    and never a v3-only audio tag on a model that would speak it aloud."""

    def _naturalness(self, breathing=True):
        from shared.orchestration.naturalness import SpeechNaturalnessPlanner
        return SpeechNaturalnessPlanner({
            "enabled": True, "breathing": breathing, "sentence_breaths": True,
            "latency_fillers": True, "filler_words": True,
            "acknowledgements": False, "backchannels": False,
            "thinking_fillers": False, "latency_filler_ladder": False,
            "sentence_breath_probability": 1.0,
        })

    async def test_v3_conversational_breathes_natively(self, monkeypatch):
        monkeypatch.setenv("TEST_EL_V3_KEY", API_KEY)
        async with MockElevenLabsDialogueServer(chunks=2, tail_chunks=1) as server:
            monkeypatch.setattr(v3_ws, "_WS_BASE", server.url)
            router = StreamingTTSRouter(
                tts_config=v3_config(native_breathing=True),
                language="hi-IN", sample_rate=8000,
                recorder=make_recorder("vs_native_on"),
                naturalness=self._naturalness(True),
            )
            # Deterministic: always breathe.
            monkeypatch.setattr(v3_ws, "_DEFAULT_BREATH_PROBABILITY", 1.0)
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, ["नमस्ते जी। ", "आपका ऑर्डर कल आएगा।"])
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        texts = server.texts()
        assert texts, "no text reached the provider"
        assert texts[0].startswith("[exhales] "), texts
        # Exactly one breath for the reply, never one per sentence.
        assert sum(t.count("[exhales]") for t in texts) == 1, texts
        # The spoken words are unchanged.
        assert "नमस्ते जी।" in texts[0]

    async def test_breathing_off_sends_no_tag(self, monkeypatch):
        """Default OFF: with the setting absent, no tag is ever sent — even
        with the Natural Conversation Breathing control fully ON. The two
        controls are independent."""
        monkeypatch.setenv("TEST_EL_V3_KEY", API_KEY)
        async with MockElevenLabsDialogueServer(chunks=2, tail_chunks=1) as server:
            monkeypatch.setattr(v3_ws, "_WS_BASE", server.url)
            monkeypatch.setattr(v3_ws, "_DEFAULT_BREATH_PROBABILITY", 1.0)
            router = StreamingTTSRouter(
                tts_config=v3_config(), language="hi-IN", sample_rate=8000,
                recorder=make_recorder("vs_native_off"),
                naturalness=self._naturalness(True),   # their Breathing is ON
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, ["नमस्ते जी।"])
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        assert all("[exhales]" not in t for t in server.texts()), server.texts()

    async def test_flash_never_receives_an_audio_tag(self, monkeypatch):
        """Flash v2.5 would SPEAK "[exhales]" aloud. The tag must be
        structurally unreachable for it, even with Breathing ON."""
        import shared.providers.tts.elevenlabs_ws as flash_ws
        from tests.mock_tts_servers import MockElevenLabsServer

        monkeypatch.setenv("TEST_EL_V3_KEY", API_KEY)
        async with MockElevenLabsServer(chunks=2) as server:
            monkeypatch.setattr(flash_ws, "_WS_BASE", server.url)
            monkeypatch.setattr(v3_ws, "_DEFAULT_BREATH_PROBABILITY", 1.0)
            router = StreamingTTSRouter(
                tts_config=v3_config(model="eleven_flash_v2_5"),
                language="hi-IN", sample_rate=8000,
                recorder=make_recorder("vs_native_flash"),
                naturalness=self._naturalness(True),
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, ["नमस्ते जी।"])
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        joined = " ".join(server.texts())
        assert "[exhales]" not in joined, joined
        assert "exhales" not in joined, joined

    async def test_per_language_override_decides_on_its_own_model(self, monkeypatch):
        """Default engine is Flash; the ml-IN override is v3 Conversational.
        Only the override's engine may breathe natively."""
        monkeypatch.setenv("TEST_EL_V3_KEY", API_KEY)
        import shared.providers.tts.elevenlabs_ws as flash_ws
        from tests.mock_tts_servers import MockElevenLabsServer
        from voice_runtime.frames import SwitchVoiceLanguageFrame

        async with MockElevenLabsServer(chunks=2) as flash_server, \
                MockElevenLabsDialogueServer(chunks=2, tail_chunks=1) as v3_server:
            monkeypatch.setattr(flash_ws, "_WS_BASE", flash_server.url)
            monkeypatch.setattr(v3_ws, "_WS_BASE", v3_server.url)
            monkeypatch.setattr(v3_ws, "_DEFAULT_BREATH_PROBABILITY", 1.0)
            router = StreamingTTSRouter(
                tts_config=v3_config(model="eleven_flash_v2_5",
                                     native_breathing=True, language_map={
                    "ml-IN": {"provider": "elevenlabs", "model": MODEL,
                              "voice": "monika-wire-id",
                              "params": {"stability": 0.5, "native_breathing": True},
                              "api_key_reference": "env:TEST_EL_V3_KEY"},
                }),
                language="hi-IN", sample_rate=8000,
                recorder=make_recorder("vs_native_lang"),
                naturalness=self._naturalness(True),
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, ["नमस्ते जी।"])              # Flash
                await worker.queue_frame(SwitchVoiceLanguageFrame(language="ml-IN"))
                await asyncio.sleep(0.1)
                await speak_turn(worker, ["നമസ്കാരം."])               # v3conv
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        assert all("exhales" not in t for t in flash_server.texts()), flash_server.texts()
        assert any(t.startswith("[exhales] ") for t in v3_server.texts()), v3_server.texts()

    async def test_pause_mode_still_breathes_once_per_reply(self, monkeypatch):
        """Pause mode dispatches every sentence as its OWN provider context.
        A per-context guard would therefore tag each sentence — three breaths
        in one reply, and roughly three times the added latency."""
        monkeypatch.setenv("TEST_EL_V3_KEY", API_KEY)
        async with MockElevenLabsDialogueServer(chunks=1, tail_chunks=1) as server:
            monkeypatch.setattr(v3_ws, "_WS_BASE", server.url)
            monkeypatch.setattr(v3_ws, "_DEFAULT_BREATH_PROBABILITY", 1.0)
            router = StreamingTTSRouter(
                tts_config=v3_config(native_breathing=True),
                language="hi-IN", sample_rate=8000,
                pause_ms=350,                      # <- pause mode
                recorder=make_recorder("vs_native_pause"),
                naturalness=self._naturalness(True),
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, [
                    "पहला वाक्य यहाँ है। ",
                    "दूसरा वाक्य अब आता है। ",
                    "और यह आखिरी वाक्य है।",
                ], settle=2.5)
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        texts = server.texts()
        assert len(texts) >= 3, texts
        breaths = sum(t.count("[exhales]") for t in texts)
        assert breaths == 1, f"expected one breath per reply, got {breaths}: {texts}"
        assert texts[0].startswith("[exhales] "), texts[0]
