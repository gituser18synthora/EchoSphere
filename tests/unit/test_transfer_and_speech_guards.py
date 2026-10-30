"""Transfer-lifecycle guards + spoken-text safety (placeholder/gender).

- A requested transfer must never be followed by the bot's own normal
  end-call path (the media socket would close while the FreeSWITCH dialplan
  is still executing the transfer), and a second transfer control is never
  queued for the same call.
- Unresolved template placeholders are stripped at the TTS boundary — the
  last line of defense, whatever produced the text.
- Configured voice gender rewrites authored Hindi first-person grammar,
  including subject-drop sentences.
"""

from pipecat.frames.frames import EndWorkerFrame

from shared.orchestration.voice_identity import (
    VoiceIdentity,
    adapt_authored_speaker_grammar,
)
from voice_runtime.brain import ConversationBrain

from tests.unit.test_tts_router_call_stability import (
    FakeProvider,
    FakeRecorder,
    _run_tts,
    make_router,
)


class _Recorder:
    def __init__(self):
        self.events = []

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    def flush_event_soon(self, kind, **data):
        self.events.append((kind, data))

    async def flush_event(self, kind, **data):
        self.events.append((kind, data))


def _transfer_brain():
    brain = ConversationBrain.__new__(ConversationBrain)
    brain._closing = False
    brain._transfer_requested = True
    brain._policy = None
    brain._recorder = _Recorder()
    brain._conversation_language = "hi-IN"
    brain.pushed = []
    brain.said = []
    brain.controls = []

    async def push(frame, direction=None):
        brain.pushed.append(frame)

    async def say(text, **kwargs):
        brain.said.append(text)

    brain.push_frame = push
    brain._say = say
    brain._queue_control = lambda payload: brain.controls.append(payload)
    return brain


class TestTransferLifecycle:
    async def test_no_policy_close_while_transfer_pending(self):
        brain = _transfer_brain()
        await brain._close_call_completed("escalated_to_agent")
        assert not [f for f in brain.pushed if isinstance(f, EndWorkerFrame)]
        assert brain._closing is False
        assert any(
            kind == "close_skipped_transfer_pending"
            for kind, _ in brain._recorder.events
        )

    async def test_duplicate_handoff_never_requeues_control(self):
        from shared.orchestration.router import RouteDecision, RouteKind

        brain = _transfer_brain()
        decision = RouteDecision(
            kind=RouteKind.HANDOFF, action="transfer", reason="agent_request"
        )
        await brain._handle_handoff(decision)
        # Already-requested transfer: reassurance only, no second control.
        assert brain.controls == []
        assert brain.said  # the caller still hears a "connecting you" line
        assert any(
            kind == "handoff_duplicate_suppressed"
            for kind, _ in brain._recorder.events
        )

    async def test_first_handoff_queues_exactly_one_control(self):
        from shared.orchestration.router import RouteDecision, RouteKind

        brain = _transfer_brain()
        brain._transfer_requested = False
        decision = RouteDecision(
            kind=RouteKind.HANDOFF, action="transfer", reason="agent_request"
        )
        await brain._handle_handoff(decision)
        assert brain._transfer_requested is True
        assert len(brain.controls) == 1
        assert brain.controls[0]["event"] == "transfer"


class TestPlaceholderGuardAtTTS:
    async def test_unresolved_placeholder_never_reaches_provider(self):
        recorder = FakeRecorder()
        router = make_router(pause_ms=200, recorder=recorder)
        provider = FakeProvider()

        async def fake_get_provider(engine, locale):
            return provider

        router._get_provider = fake_get_provider
        await _run_tts(
            router, "नमस्ते {customer_name} ji, aapka payment due hai.", "ctx_ph"
        )
        texts = [call[2] for call in provider.calls if call[0] == "text"]
        assert texts, "the speakable remainder must still be spoken"
        assert "{" not in texts[0] and "customer_name" not in texts[0]
        assert any(
            e["kind"] == "tts_placeholder_stripped" for e in recorder.events
        )

    async def test_placeholder_only_segment_is_skipped_entirely(self):
        recorder = FakeRecorder()
        router = make_router(pause_ms=200, recorder=recorder)
        provider = FakeProvider()

        async def fake_get_provider(engine, locale):
            return provider

        router._get_provider = fake_get_provider
        await _run_tts(router, "{voice_speaker_name}", "ctx_ph2")
        assert provider.calls == []

    async def test_dotted_placeholder_is_stripped(self):
        recorder = FakeRecorder()
        router = make_router(pause_ms=200, recorder=recorder)
        provider = FakeProvider()

        async def fake_get_provider(engine, locale):
            return provider

        router._get_provider = fake_get_provider
        await _run_tts(router, "Hello {customer.name}, payment due hai.", "ctx_ph3")
        texts = [call[2] for call in provider.calls if call[0] == "text"]
        assert texts and "customer.name" not in texts[0]


class TestVoiceGenderAgreement:
    def test_female_voice_rewrites_authored_masculine_hindi(self):
        female = VoiceIdentity(name="Neha", gender="female")
        assert adapt_authored_speaker_grammar(
            "मैं समझ सकता हूँ।", female
        ) == "मैं समझ सकती हूँ।"
        assert adapt_authored_speaker_grammar(
            "मैं आपको बताता हूँ, मैं देख लेता हूँ।", female
        ) == "मैं आपको बताती हूँ, मैं देख लेती हूँ।"

    def test_subject_drop_first_person_is_covered(self):
        male = VoiceIdentity(name="Rahul", gender="male")
        assert adapt_authored_speaker_grammar(
            "कल call कर रही हूँ।", male
        ) == "कल call कर रहा हूँ।"

    def test_customer_directed_grammar_untouched(self):
        male = VoiceIdentity(name="Rahul", gender="male")
        text = "क्या आप payment कर रही हैं?"
        assert adapt_authored_speaker_grammar(text, male) == text

    def test_neutral_gender_changes_nothing(self):
        neutral = VoiceIdentity(name="X", gender="neutral")
        text = "मैं समझ सकता हूँ।"
        assert adapt_authored_speaker_grammar(text, neutral) == text
