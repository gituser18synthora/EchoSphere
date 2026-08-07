"""Turn-latency event ownership — cross-turn marks can no longer contaminate.

Regressions pinned here (all observed in production logs):

- a merged/re-dispatched turn inherited the cancelled generation's classify/
  LLM marks and reported NEGATIVE classify and llm_first_token spans;
- greeting TTS timestamps leaked into the first caller turn;
- late TTS bytes of a cancelled reply stamped ``tts_first_byte`` into the next
  caller turn (stale TTS timestamps);
- bot audio of the PREVIOUS reply, arriving after the caller had already
  started a new utterance (barge-in race), closed the new turn's ``response``
  span against the wrong reply;
- a prefetched decision finishing before dispatch looked like a measurement
  gap instead of an overlap win.

The fixes are ownership/association fixes (marks are cleared or refused at the
turn boundary), not clamps — the snapshot's negative filter is a logged
tripwire for unmodelled races only.
"""

import asyncio
import time

from pipecat.frames.frames import BotStartedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection

from voice_runtime.turn_metrics import TurnLatencyTracker

from tests.unit.test_turn_endpointing import make_brain, stub_turn_handler


class TestDispatchOwnsTheStageMarks:
    def test_redispatch_clears_previous_generation_marks(self):
        """A merged turn re-dispatches; the cancelled turn's classify/LLM/TTS
        marks must not survive into it as negative spans."""
        tracker = TurnLatencyTracker(session_id="s")
        tracker.mark_speech_started()
        tracker.mark_speech_stopped()
        tracker.mark_dispatched()
        tracker.mark_classified()
        tracker.mark_llm_request()
        tracker.mark_llm_first_token()
        tracker.mark_tts_request()
        tracker.mark_tts_first_byte()

        # Straggler final → cancel + rewind + re-dispatch of the merged turn.
        tracker.mark_dispatched()
        spans = tracker.snapshot()
        for stage in ("classify", "llm_ttft", "llm_first_token",
                      "tts_ttfb", "tts_queue", "playout"):
            assert stage not in spans, f"stale {stage} span survived re-dispatch"
        assert all(value >= 0 for value in spans.values())
        assert "negative_span_dropped" not in tracker.counts

    def test_redispatched_turn_reports_its_own_fresh_stages(self):
        tracker = TurnLatencyTracker(session_id="s")
        tracker.mark_dispatched()
        tracker.mark_classified()          # cancelled generation's mark
        tracker.mark_dispatched()          # merged turn takes over
        tracker.mark_classified()          # its own decision completes
        spans = tracker.snapshot()
        assert spans["classify"] >= 0

    def test_redispatch_resets_the_reported_flag(self):
        """Held segments can dispatch a new turn with no new VAD start; the
        previous turn's report must not swallow the new one."""
        tracker = TurnLatencyTracker(session_id="s")
        tracker.mark_dispatched()
        tracker.reported = True
        tracker.mark_dispatched()
        assert tracker.reported is False


class TestGreetingOverlap:
    def test_greeting_tts_marks_do_not_leak_into_the_first_turn(self):
        tracker = TurnLatencyTracker(session_id="s")
        # Greeting: synthesis with no dispatched caller turn.
        tracker.mark_tts_request()
        tracker.mark_tts_first_byte()
        tracker.mark_bot_started_speaking()
        assert tracker.bot_started_at is not None  # greeting owns its audio

        # First caller utterance arrives.
        tracker.mark_speech_started()
        tracker.mark_speech_stopped()
        tracker.mark_dispatched()
        spans = tracker.snapshot()
        for stage in ("tts_ttfb", "playout", "tts_first_audio", "response"):
            assert stage not in spans, f"greeting {stage} leaked into turn 1"
        assert all(value >= 0 for value in spans.values())


class TestLateTtsAudio:
    def test_first_byte_without_a_request_is_refused_and_counted(self):
        """Late audio from a cancelled synthesis context must not stamp a
        timestamp into the turn now in flight."""
        tracker = TurnLatencyTracker(session_id="s")
        tracker.mark_dispatched()          # new turn, no synthesis yet
        tracker.mark_tts_first_byte()      # stale byte from the old context
        assert tracker.tts_first_byte_at is None
        assert tracker.counts.get("tts_byte_without_request") == 1

    def test_owned_first_byte_still_measures(self):
        tracker = TurnLatencyTracker(session_id="s")
        tracker.mark_dispatched()
        tracker.mark_tts_request()
        tracker.mark_tts_first_byte()
        assert tracker.snapshot()["tts_ttfb"] >= 0


class TestBargeInAudioRace:
    def test_previous_reply_audio_cannot_close_the_new_turn(self):
        tracker = TurnLatencyTracker(session_id="s")
        # Caller barges in: the VAD probe resets the tracker …
        tracker.mark_speech_started()
        # … and the tail of the PREVIOUS reply still reaches the transport.
        tracker.mark_bot_started_speaking()
        assert tracker.bot_started_at is None
        assert tracker.counts.get("bot_audio_without_turn") == 1
        tracker.mark_speech_stopped()
        assert "response" not in tracker.snapshot()

    async def test_brain_does_not_report_latency_for_disowned_audio(self):
        tracker = TurnLatencyTracker(session_id="s")
        brain = make_brain(latency=tracker)
        stub_turn_handler(brain)
        tracker.mark_speech_started()  # new utterance already begun
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        kinds = [k for k, _ in brain._recorder.events]
        assert "turn_latency" not in kinds
        assert tracker.reported is False  # the real reply can still report

    async def test_real_reply_still_reports_once(self):
        tracker = TurnLatencyTracker(session_id="s")
        brain = make_brain(latency=tracker)
        stub_turn_handler(brain)
        tracker.mark_speech_started()
        tracker.mark_speech_stopped()
        tracker.mark_dispatched()
        tracker.mark_tts_request()
        tracker.mark_tts_first_byte()
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        kinds = [k for k, _ in brain._recorder.events]
        assert kinds.count("turn_latency") == 1
        spans = brain._recorder.event("turn_latency")
        assert spans["response"] >= 0


class TestPrefetchRepresentation:
    async def test_prefetch_hit_is_counted_not_negative(self):
        from tests.unit.test_agentic_orchestration import (
            _AgenticLLMStub,
            make_agentic_brain,
        )
        from tests.unit.test_brain_collection_policy import snapshot

        llm = _AgenticLLMStub([{
            "scope": "in_scope", "confidence": 0.9, "next_action": "answer",
            "response_text": "जी बताइए।",
        }])
        brain = make_agentic_brain(context=snapshot(), llm=llm, verified=True)
        text = "मुझे जानकारी चाहिए payment के बारे में"
        brain._latency.mark_speech_started()
        brain._latency.mark_speech_stopped()
        # The endpoint wait starts the speculative decision …
        brain._pending_segments = [text]
        brain._start_decision_prefetch()
        assert brain._decision_prefetch is not None
        await brain._decision_prefetch[1]  # completes BEFORE dispatch
        # … and the dispatched turn consumes it.
        brain._latency.mark_dispatched()
        decision = await brain._take_decision(text)
        assert decision is not None
        assert brain._latency.counts.get("decision_prefetched") == 1
        spans = brain._latency.snapshot()
        assert spans["classify"] >= 0


class TestNegativeSpanTripwire:
    def test_unmodelled_negative_span_is_dropped_and_counted(self):
        tracker = TurnLatencyTracker(session_id="s")
        now = time.monotonic()
        tracker.dispatched_at = now
        tracker.classified_at = now - 1.0  # deliberately corrupted ownership
        spans = tracker.snapshot()
        assert "classify" not in spans
        assert tracker.counts.get("negative_span_dropped") == 1


if __name__ == "__main__":  # pragma: no cover
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
