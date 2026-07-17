"""Pipecat pipeline with test providers: turns, KB grounding, barge-in."""

import asyncio

import pytest
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    TextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.workers.runner import WorkerRunner
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from backend.knowledge.schemas import RetrievalResult, SourceRef
from backend.providers.base import ProviderConfig
from backend.providers.factory import get_llm_provider, get_tts_provider
from backend.voice_runtime.bot_config import ResolvedBotConfig
from backend.voice_runtime.brain import ConversationBrain
from backend.voice_runtime.services import EchoTTSService
from backend.voice_runtime.session import SessionRecorder

pytestmark = pytest.mark.integration


class Collector(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.audio_frames = 0
        self.texts: list[str] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSAudioRawFrame):
            self.audio_frames += 1
        elif isinstance(frame, TextFrame):
            self.texts.append(frame.text)
        await self.push_frame(frame, direction)


class StubKnowledge:
    """Returns a canned grounded result for KB-routed turns."""

    def __init__(self):
        self.calls = 0

    async def search(self, request):
        self.calls += 1
        source = SourceRef(
            kb_id="kb-1", document_id="doc-1", chunk_id="chunk-4", chunk_index=4,
            page_number=7, section="Renewal", score=0.88, vector_score=0.88,
            text="The policy grace period is 30 days.", document_name="policy.pdf",
        )
        return RetrievalResult(
            used_knowledge_base=True, answerable=True, confidence=0.88,
            query=request.query, kb_ids=["kb-1"], sources=[source], duration_ms=12.0,
        )


class SlowLLM:
    """Streams slowly so barge-in cancellation is observable."""

    name = "slow-llm"

    async def generate(self, messages, **kwargs):
        raise NotImplementedError

    async def stream(self, messages, **kwargs):
        for index in range(50):
            await asyncio.sleep(0.05)
            yield f"token{index} "

    async def health_check(self):
        return {"ok": True}


def make_config(**overrides) -> ResolvedBotConfig:
    defaults = dict(
        tenant_id="tn-001", bot_id="bot-101", bot_name="TestBot", version="v1",
        published=True, greeting="Hello from TestBot.",
        system_prompt="Be brief.", stt={"provider": "mock"},
        tts={"provider": "mock"}, llm={"provider": "mock"}, kb_ids=["kb-1"],
    )
    defaults.update(overrides)
    return ResolvedBotConfig(**defaults)


async def run_call(brain, tts, collector, feeder):
    pipeline = Pipeline([brain, tts, collector])
    worker = PipelineWorker(
        pipeline, params=PipelineParams(enable_metrics=False),
        enable_rtvi=False, idle_timeout_secs=None,
    )
    runner = WorkerRunner(handle_sigint=False)
    feed_task = asyncio.create_task(feeder(worker, brain))
    await asyncio.wait_for(runner.run(worker), timeout=30)
    await feed_task


class TestKnowledgeGrounding:
    async def test_kb_question_uses_sources(self):
        config = make_config()
        recorder = SessionRecorder("vs_test_kb", config)
        knowledge = StubKnowledge()
        brain = ConversationBrain(
            config=config, llm=get_llm_provider(ProviderConfig(provider="mock")),
            recorder=recorder, knowledge_service=knowledge,
        )
        tts = EchoTTSService(get_tts_provider(ProviderConfig(provider="mock")),
                             sample_rate=16000)
        collector = Collector()

        async def feeder(worker, brain):
            await asyncio.sleep(0.2)
            await worker.queue_frame(
                TranscriptionFrame("what is the policy grace period", "caller", "t1")
            )
            await asyncio.sleep(0.8)
            await worker.queue_frame(TranscriptionFrame("goodbye, hang up", "caller", "t2"))

        await run_call(brain, tts, collector, feeder)

        assert knowledge.calls == 1
        bot_turns = [t for t in recorder.turns if t.role == "bot" and t.kb_used]
        assert bot_turns and bot_turns[0].kb_sources[0]["chunkId"] == "chunk-4"
        assert "30 days" in " ".join(collector.texts)
        route_events = [e for e in recorder.events if e["kind"] == "route_decision"]
        assert route_events[0]["route"] == "knowledge"

    async def test_greeting_skips_kb(self):
        config = make_config()
        recorder = SessionRecorder("vs_test_greet", config)
        knowledge = StubKnowledge()
        brain = ConversationBrain(
            config=config, llm=get_llm_provider(ProviderConfig(provider="mock")),
            recorder=recorder, knowledge_service=knowledge,
        )
        tts = EchoTTSService(get_tts_provider(ProviderConfig(provider="mock")),
                             sample_rate=16000)
        collector = Collector()

        async def feeder(worker, brain):
            await asyncio.sleep(0.2)
            await worker.queue_frame(TranscriptionFrame("hello there", "caller", "t1"))
            await asyncio.sleep(0.5)
            await worker.queue_frame(TranscriptionFrame("hang up", "caller", "t2"))

        await run_call(brain, tts, collector, feeder)
        assert knowledge.calls == 0
        route_events = [e for e in recorder.events if e["kind"] == "route_decision"]
        assert route_events[0]["route"] == "chat"
        assert route_events[0]["reason"] == "smalltalk"


class TestBargeIn:
    async def test_interruption_cancels_generation(self):
        config = make_config(kb_ids=[])
        recorder = SessionRecorder("vs_test_barge", config)
        brain = ConversationBrain(config=config, llm=SlowLLM(), recorder=recorder)
        tts = EchoTTSService(get_tts_provider(ProviderConfig(provider="mock")),
                             sample_rate=16000)
        collector = Collector()

        async def feeder(worker, brain):
            await asyncio.sleep(0.2)
            await worker.queue_frame(
                TranscriptionFrame("tell me a very long story about anything", "caller", "t1")
            )
            await asyncio.sleep(0.5)  # a few slow tokens flow
            await worker.queue_frame(InterruptionFrame())
            await asyncio.sleep(0.3)
            await worker.queue_frame(TranscriptionFrame("hang up now please", "caller", "t2"))

        await run_call(brain, tts, collector, feeder)

        cancelled = [e for e in recorder.events if e["kind"] == "generation_cancelled"]
        assert cancelled, "barge-in did not cancel generation"
        # The slow stream (50 tokens * 50ms) was cut off well before completion.
        streamed = [t for t in collector.texts if t.startswith("token")]
        assert len(streamed) < 50
