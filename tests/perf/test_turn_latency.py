"""Measured turn-detection dead time: end of caller speech -> LLM dispatch.

Wires the REAL pipecat turn-stop strategy to a REAL ConversationBrain exactly
as ``build_voice_pipeline`` does, and measures with real timers. Provider time
(LLM, TTS, network) is deliberately excluded so what is measured is the dead
time the platform itself adds — the part this work changed, and the part that
regressions hide in.

Prints the numbers and asserts generous ceilings, following the convention of
the other perf tests: the assertions guard the policy, the printed figures are
the evidence.

Run: env/bin/python -m pytest tests/perf/test_turn_latency.py -m perf -s
"""

import asyncio
import time

import pytest
from pipecat.frames.frames import (
    STTMetadataFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.stt_latency import SARVAM_TTFS_P99
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.utils.asyncio.task_manager import TaskManager

import voice_runtime.brain as brain_module
from shared.bot_config import ResolvedBotConfig
from shared.turn_detection import TURN_DETECTION_DEFAULTS
from voice_runtime.brain import ConversationBrain
from voice_runtime.endpointing import utterance_looks_complete

pytestmark = pytest.mark.perf

# Time from the STT flush (at VAD stop) to the final landing. Sarvam's own
# published P99 is 1.17 s measured at stop_secs=0.8; 0.35 s is a representative
# typical delivery and is held constant across before/after so the comparison
# isolates the policy, not the provider.
STT_LAG = 0.35

COMPLETE_SENTENCE = "मैं कल पेमेंट कर दूंगा."
SHORT_REPLY = "हाँ"
MID_THOUGHT = "नहीं मेरे पास अभी"


class _Recorder:
    def __init__(self):
        self.events, self.turns = [], []
        self.session_id = "s-perf"
        self.usage = {"kb_searches": 0, "llm_output_tokens": 0}
        self.language = "hi-IN"

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    def add_turn(self, turn):
        self.turns.append(turn)

    async def flush_event(self, kind, **data):
        self.events.append((kind, data))


def _make_brain(*, finalize_grace, finalize_settle, complete_endpoint):
    config = ResolvedBotConfig(
        tenant_id="t", bot_id="b", bot_name="n", version="1", published=True,
        language="hi-IN", languages=["hi-IN"], stt={"provider": "sarvam"},
        system_prompt="test",
    )
    brain = ConversationBrain(
        config=config, llm=None, recorder=_Recorder(),
        finalize_grace=finalize_grace, finalize_settle=finalize_settle,
        complete_endpoint=complete_endpoint,
    )
    dispatched = asyncio.Event()

    async def _handle(_text):
        dispatched.set()

    async def _noop(*_a, **_k):
        pass

    brain._handle_turn = _handle
    brain.push_frame = _noop
    brain._notify_client = _noop
    brain.create_task = lambda coro, name=None: asyncio.get_event_loop().create_task(coro)

    async def _cancel(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    brain.cancel_task = _cancel
    return brain, dispatched


async def dead_time(*, transport: str, text: str, adaptive: bool,
                    monkeypatch=None) -> float:
    """VAD stop -> brain dispatches the turn, in seconds.

    ``adaptive=False`` reproduces the shipped behaviour before this work: the
    STT never reported finality, the finalize debounce always ran in full, and
    no utterance was ever endpointed early.
    """
    defaults = TURN_DETECTION_DEFAULTS[transport]

    if adaptive:
        brain, dispatched = _make_brain(
            finalize_grace=defaults["finalize_grace"],
            finalize_settle=defaults["finalize_settle"],
            complete_endpoint=defaults["complete_endpoint"],
        )
        brain_module.utterance_looks_complete = utterance_looks_complete
    else:
        brain, dispatched = _make_brain(
            finalize_grace=defaults["finalize_grace"],
            finalize_settle=0.0,
            complete_endpoint=defaults["complete_endpoint"],
        )
        brain._settled_grace = lambda: defaults["finalize_grace"]
        brain_module.utterance_looks_complete = lambda _text: False

    manager = TaskManager()
    strategy = SpeechTimeoutUserTurnStopStrategy(
        user_speech_timeout=defaults["user_speech_timeout"], wait_for_transcript=False
    )
    await strategy.setup(manager)

    async def _close_turn():
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    strategy.add_event_handler(
        "on_user_turn_stopped", lambda *a, **k: asyncio.ensure_future(_close_turn())
    )

    await strategy.process_frame(
        STTMetadataFrame(service_name="sarvam", ttfs_p99_latency=SARVAM_TTFS_P99)
    )
    await strategy.process_frame(VADUserStartedSpeakingFrame())
    await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    start = time.monotonic()
    await strategy.process_frame(
        VADUserStoppedSpeakingFrame(stop_secs=defaults["stop_secs"])
    )

    async def deliver_final():
        await asyncio.sleep(STT_LAG)
        frame = TranscriptionFrame(text, "caller", f"ts-{time.monotonic()}",
                                   language="hi-IN")
        frame.finalized = adaptive  # the shipped service never set this
        await strategy.process_frame(frame)
        await brain.process_frame(frame, FrameDirection.DOWNSTREAM)

    task = asyncio.ensure_future(deliver_final())
    try:
        await asyncio.wait_for(dispatched.wait(), 10)
    finally:
        await task
        await strategy.cleanup()
        brain_module.utterance_looks_complete = utterance_looks_complete
    return time.monotonic() - start


CASES = [
    ("telephony", COMPLETE_SENTENCE, "complete sentence"),
    ("telephony", SHORT_REPLY, "short reply"),
    ("telephony", MID_THOUGHT, "mid-thought pause"),
    ("browser", COMPLETE_SENTENCE, "complete sentence"),
    ("browser", SHORT_REPLY, "short reply"),
    ("browser", MID_THOUGHT, "mid-thought pause"),
]


class TestEndOfSpeechToResponse:
    async def test_measured_dead_time_before_and_after(self, capsys):
        rows = []
        for transport, text, label in CASES:
            before = await dead_time(transport=transport, text=text, adaptive=False)
            after = await dead_time(transport=transport, text=text, adaptive=True)
            rows.append((transport, label, before, after))

        with capsys.disabled():
            print("\n  end of caller speech -> LLM dispatch "
                  f"(stop_secs included; stt lag {STT_LAG * 1000:.0f}ms)")
            print(f"  {'transport':<11}{'case':<20}{'BEFORE':>9}{'AFTER':>9}{'delta':>9}")
            for transport, label, before, after in rows:
                stop = TURN_DETECTION_DEFAULTS[transport]["stop_secs"]
                print(f"  {transport:<11}{label:<20}"
                      f"{(before + stop) * 1000:>7.0f}ms{(after + stop) * 1000:>7.0f}ms"
                      f"{(after - before) * 1000:>+7.0f}ms")

        for transport, label, before, after in rows:
            assert after < before, f"{transport}/{label} did not improve"

    async def test_telephony_complete_utterance_answers_within_a_second(self):
        elapsed = await dead_time(
            transport="telephony", text=COMPLETE_SENTENCE, adaptive=True
        )
        total = elapsed + TURN_DETECTION_DEFAULTS["telephony"]["stop_secs"]
        # Generous ceiling: the policy target is ~0.85 s of dead time.
        assert total < 1.2, f"telephony dead time regressed to {total * 1000:.0f}ms"

    async def test_short_reply_is_at_least_as_fast_as_a_full_sentence(self):
        sentence = await dead_time(
            transport="telephony", text=COMPLETE_SENTENCE, adaptive=True
        )
        short = await dead_time(
            transport="telephony", text=SHORT_REPLY, adaptive=True
        )
        assert short <= sentence + 0.05

    async def test_mid_thought_pause_still_gets_the_full_window(self):
        # The guard rail: the latency work must NOT have bought speed by
        # shortening the pause a caller is allowed mid-sentence.
        defaults = TURN_DETECTION_DEFAULTS["telephony"]
        elapsed = await dead_time(
            transport="telephony", text=MID_THOUGHT, adaptive=True
        )
        assert elapsed >= defaults["user_speech_timeout"] - 0.05
