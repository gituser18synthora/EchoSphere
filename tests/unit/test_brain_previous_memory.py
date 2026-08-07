"""Previous-call memory inside the live brain.

Pins that memory is CONTEXT, never current truth: the prompt block carries an
explicit precedence rule, the Goal Engine's live state gets a compact
previous-call line, language continuity only selects the STARTING locale
(per-turn switching still owns the conversation), and the opening line is
generated under the bot's own prompt with the authored greeting as the
always-available fallback.
"""

import asyncio
from datetime import datetime, timezone

from shared.bot_config import ResolvedBotConfig
from shared.post_call.recall import PreviousCallMemory
from shared.post_call.schema import PostCallAnalysis
from voice_runtime.brain import ConversationBrain

from tests.unit.test_brain_collection_policy import _RecorderStub, _StreamingLLMStub


def make_memory(**overrides) -> PreviousCallMemory:
    analysis = PostCallAnalysis.model_validate({
        "call_outcome": "refused_to_pay",
        "summary": "Customer confirmed identity but said payment was not "
                   "possible that day.",
        "important_facts": ["Customer gets salary on the 10th"],
        "unresolved_items": ["payment_commitment", "payment_date"],
        "customer_commitments": [{
            "type": "payment", "amount": 2000, "due_date": "2026-08-10",
            "status": "promised", "description": "pay two thousand on Monday",
        }],
        "last_customer_language": overrides.pop("last_customer_language", "hi-IN"),
    })
    fields = dict(
        conversation_id="cv_prev1",
        session_id="vs_prev1",
        started_at=datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc),
        status="completed",
        call_outcome="refused_to_pay",
        summary=analysis.summary,
        analysis=analysis,
        next_action="follow_up_on_commitment",
        next_action_reason="Open ₹2,000 commitment",
        follow_up_at=None,
        language="hi-IN",
        dominant_language="hi-IN",
        open_commitments=[c.model_dump(mode="json")
                          for c in analysis.customer_commitments],
    )
    fields.update(overrides)
    return PreviousCallMemory(**fields)


def make_brain(memory=None, *, language="hi-IN",
               languages=("en-IN", "hi-IN"), llm=None) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language=language, languages=list(languages),
        stt={"provider": "sarvam"}, system_prompt="You are Recovery Bot.",
        greeting="नमस्कार! क्या मेरी बात Ramesh जी से हो रही है?",
    )
    brain = ConversationBrain(
        config=config, llm=llm or _StreamingLLMStub(),
        recorder=_RecorderStub(), previous_memory=memory,
    )
    brain._pushed = []
    brain._notified = []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        brain._notified.append(payload)

    brain.push_frame = _push
    brain._notify_client = _notify

    def _create_task(coro, name=None):
        return asyncio.get_event_loop().create_task(coro)

    brain.create_task = _create_task
    return brain


class TestMemoryPromptBlock:
    def test_memory_reaches_the_system_prompt(self):
        brain = make_brain(make_memory())
        system = brain._static_system
        assert "Previous conversation memory" in system
        assert "refused_to_pay" in system
        assert "pay two thousand on Monday" in system
        assert "payment_commitment" in system
        assert "salary on the 10th" in system

    def test_precedence_rule_is_explicit(self):
        system = make_brain(make_memory())._static_system
        assert "NOT current truth" in system
        # Current turn / verified data must be stated as overriding.
        assert "override this memory" in system

    def test_no_memory_no_block(self):
        assert "Previous conversation memory" not in make_brain(None)._static_system

    def test_goal_engine_live_state_carries_previous_call(self):
        brain = make_brain(make_memory())
        state = brain._orchestration_state()
        assert "previous_call" in state
        assert "2000" in state["previous_call"]
        assert "pending" in state["previous_call"]

    def test_memory_never_marks_claims_verified(self):
        memory = make_memory()
        assert all(
            c.get("status") != "verified" for c in memory.open_commitments
        )


class TestLanguageContinuity:
    def test_previous_english_caller_starts_in_english(self):
        brain = make_brain(make_memory(last_customer_language="en-IN"))
        assert brain._conversation_language == "en-IN"
        assert brain._recorder.language == "en-IN"

    def test_unsupported_remembered_language_keeps_default(self):
        brain = make_brain(make_memory(last_customer_language="ta-IN"))
        assert brain._conversation_language == "hi-IN"

    async def test_per_turn_switching_still_wins(self):
        # Continuity picks the start; the caller's ACTUAL language wins turns.
        brain = make_brain(make_memory(last_customer_language="en-IN"))
        assert brain._conversation_language == "en-IN"
        await brain._maybe_switch_language("नहीं मैं अभी नहीं करूँगा", "hi-IN")
        assert brain._conversation_language == "hi-IN"


class _GreetingLLMStub(_StreamingLLMStub):
    def __init__(self, text="Namaste Ramesh ji, pichhli baat ke follow-up "
                            "mein call kiya hai. Kya ab payment possible hai?",
                 fail=False):
        super().__init__()
        self._text = text
        self._fail = fail
        self.generate_calls = []

    async def generate(self, messages, *, system=None, temperature=None,
                       max_tokens=None, tools=None):
        self.generate_calls.append({"messages": messages, "system": system})
        if self._fail:
            raise RuntimeError("provider down")

        class _Result:
            text = self._text
            input_tokens = 50
            output_tokens = 30

        return _Result()


class TestMemoryGreeting:
    async def test_opening_is_generated_as_a_continuation(self):
        llm = _GreetingLLMStub()
        brain = make_brain(make_memory(), llm=llm)
        brain._pipeline_started = True
        await brain._open_session()
        assert llm.generate_calls, "opening must be generated, not scripted"
        system = llm.generate_calls[0]["system"]
        assert "Previous conversation memory" in system
        assert "Authored greeting (base script)" in system
        spoken = [n["text"] for n in brain._notified if n.get("type") == "bot_text"]
        assert spoken == [llm._text]
        assert ("memory_greeting_spoken" in
                [k for k, _ in brain._recorder.events])

    async def test_llm_failure_falls_back_to_authored_greeting(self):
        brain = make_brain(make_memory(), llm=_GreetingLLMStub(fail=True))
        brain._pipeline_started = True
        await brain._open_session()
        spoken = [n["text"] for n in brain._notified if n.get("type") == "bot_text"]
        assert spoken == [brain._config.greeting]

    async def test_no_memory_speaks_authored_greeting_directly(self):
        llm = _GreetingLLMStub()
        brain = make_brain(None, llm=llm)
        brain._pipeline_started = True
        await brain._open_session()
        assert llm.generate_calls == []
        spoken = [n["text"] for n in brain._notified if n.get("type") == "bot_text"]
        assert spoken == [brain._config.greeting]

    async def test_memory_greeting_can_be_disabled_per_bot(self):
        llm = _GreetingLLMStub()
        brain = make_brain(make_memory(), llm=llm)
        brain._config.llm = {"settings": {"memory_greeting_enabled": False}}
        brain._pipeline_started = True
        await brain._open_session()
        assert llm.generate_calls == []
