"""ConversationBrain guardrail wiring — the runtime side of the acceptance
criteria: a blocking guardrail stops the turn before tools/LLM, a violating
generated sentence never reaches TTS, and blocked utterances enter history
only in redacted form.
"""

from pipecat.frames.frames import TextFrame

from shared.bot_config import ResolvedBotConfig
from shared.compliance import CompliancePolicySnapshot, WordingTemplate
from shared.guardrails import (
    MANDATORY_FLOOR,
    EffectiveGuardrails,
    GuardrailEngine,
    GuardrailRule,
)
from voice_runtime.brain import ConversationBrain

from tests.unit.test_brain_collection_policy import (
    GRACE,
    _RecorderStub,
    _StreamingLLMStub,
)


def _engine() -> GuardrailEngine:
    rules = MANDATORY_FLOOR + (
        GuardrailRule("medical_advice_boundary", "Medical advice boundary",
                      "block", category="Safety"),
        GuardrailRule("payment_collection_restriction",
                      "Payment collection restriction", "block",
                      category="Compliance"),
    )
    return GuardrailEngine(EffectiveGuardrails(rules=rules))


WORDING_POLICY = CompliancePolicySnapshot(
    policy_id="cp_w", code="in_rbi_recovery_conduct", version=2,
    name="RBI recovery conduct", regulator="RBI", timezone="Asia/Kolkata",
    wordings=(
        WordingTemplate(code="recovery_notice", language="en", version=3,
                        text="This is a reminder that your loan account is overdue and a recovery notice may follow."),
    ),
)


def make_brain(*, llm=None, guardrails=None):
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="en-IN", languages=["en-IN", "hi-IN"],
        stt={"provider": "sarvam"},
        system_prompt="You are Test Bot.",
    )
    brain = ConversationBrain(
        config=config, llm=llm or _StreamingLLMStub(),
        recorder=_RecorderStub(),
        finalize_grace=GRACE, finalize_settle=GRACE, complete_endpoint=GRACE,
        short_reply_endpoint=GRACE,
        guardrails=guardrails,
    )
    brain._pushed = []
    brain._notified = []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        brain._notified.append(payload)

    brain.push_frame = _push
    brain._notify_client = _notify
    return brain


class TestInputBlocking:
    async def test_spoken_card_number_blocks_the_turn_before_the_llm(self):
        llm = _StreamingLLMStub()
        brain = make_brain(llm=llm, guardrails=_engine())

        await brain._handle_turn("my card number is 4111 1111 1111 1111")

        assert llm.calls == []  # understanding/generation never ran
        user_turns = [t for t in brain._recorder.turns if t.role == "user"]
        assert user_turns and user_turns[0].route == "guardrail"
        assert "4111" not in user_turns[0].text  # redacted in the record
        assert all("4111" not in m["content"] for m in brain._history)
        bot_turns = [t for t in brain._recorder.turns if t.role == "bot"]
        assert bot_turns and "card" in bot_turns[0].text.lower()
        assert ("guardrail_blocked_turn",
                {"stage": "input", "rules": ["payment_collection_restriction"]}
                ) in brain._recorder.events
        # The mandatory tool gate holds for the rest of the turn.
        assert brain._guardrails.check_tool_call(intent="pay").allowed is False

    async def test_ordinary_turns_are_unaffected(self):
        llm = _StreamingLLMStub(tokens=("Sure, ", "happy to help."))
        brain = make_brain(llm=llm, guardrails=_engine())

        await brain._handle_turn("what are your opening hours")

        assert llm.calls  # generation ran normally
        assert "guardrail_blocked_turn" not in brain._recorder.event_kinds()


class TestLegalWording:
    async def test_wording_reference_is_spoken_verbatim_and_version_recorded(self):
        engine = GuardrailEngine(
            EffectiveGuardrails(rules=MANDATORY_FLOOR),
            compliance=(WORDING_POLICY,),
        )
        brain = make_brain(guardrails=engine)

        await brain._say("One moment. {{wording:recovery_notice}} Please pay soon.")

        spoken = "".join(f.text for f in brain._pushed if isinstance(f, TextFrame))
        assert ("This is a reminder that your loan account is overdue and a "
                "recovery notice may follow.") in spoken
        assert "{{" not in spoken
        wording_hits = [h for h in engine.hits if h.rule.code.startswith("wording:")]
        assert wording_hits and wording_hits[0].outcome == "emitted"
        assert wording_hits[0].policy_code == "in_rbi_recovery_conduct"
        assert wording_hits[0].policy_version == 2
        assert "v3" in wording_hits[0].detail

    async def test_unresolved_wording_is_dropped_never_spoken_raw(self):
        engine = GuardrailEngine(
            EffectiveGuardrails(rules=MANDATORY_FLOOR),
            compliance=(WORDING_POLICY,),
        )
        brain = make_brain(guardrails=engine)
        await brain._say("Note. {{wording:not_a_template}} Goodbye.")
        spoken = "".join(f.text for f in brain._pushed if isinstance(f, TextFrame))
        assert "{{" not in spoken and "not_a_template" not in spoken
        assert "Goodbye" in spoken


class TestOutputBlocking:
    async def test_violating_generated_sentence_never_reaches_tts(self):
        llm = _StreamingLLMStub(
            tokens=("You should take 500 mg", " of the tablet twice a day.")
        )
        brain = make_brain(llm=llm, guardrails=_engine())

        await brain._handle_turn("what should I take for the pain")

        spoken = [f.text for f in brain._pushed if isinstance(f, TextFrame)]
        assert not any("500 mg" in s for s in spoken)
        assert any(
            kind == "guardrail_blocked_turn" and data.get("stage") == "output"
            for kind, data in brain._recorder.events
        )
        bot_turns = [t for t in brain._recorder.turns if t.role == "bot"]
        assert bot_turns and "medical" in bot_turns[-1].text.lower()

    async def test_clean_generation_streams_with_block_rules_active(self):
        llm = _StreamingLLMStub(
            tokens=("Your next appointment is on Monday. ", "Anything else?")
        )
        brain = make_brain(llm=llm, guardrails=_engine())

        await brain._handle_turn("when is my appointment")

        spoken = "".join(f.text for f in brain._pushed if isinstance(f, TextFrame))
        assert "Your next appointment is on Monday." in spoken
        assert "guardrail_blocked_turn" not in brain._recorder.event_kinds()


class TestScopeAdherencePrompt:
    """A configured guardrail profile states the bot's scope in the immutable
    prompt. This is the fallback that keeps generation in context on turns
    where the Goal Engine's scope decision never arrives (timeout, engine
    disabled) and routing defaults to in-scope — derived purely from the
    compiled goal policy, never from any specific request pattern."""

    def _brain(self, *, guardrails, use_case: str = ""):
        config = ResolvedBotConfig(
            tenant_id="tn-x", bot_id="bot-x", bot_name="Test Bot", version="v1",
            published=True, language="en-IN", languages=["en-IN", "hi-IN"],
            stt={"provider": "sarvam"},
            system_prompt="You are Test Bot.",
            use_case=use_case,
            intents=[{"name": "booking_confirmation"}, {"name": "refund_status"}],
        )
        return ConversationBrain(
            config=config, llm=_StreamingLLMStub(), recorder=_RecorderStub(),
            finalize_grace=GRACE, finalize_settle=GRACE,
            complete_endpoint=GRACE, short_reply_endpoint=GRACE,
            guardrails=guardrails,
        )

    def test_configured_profile_states_scope_in_static_prompt(self):
        brain = self._brain(
            guardrails=GuardrailEngine(EffectiveGuardrails(
                rules=MANDATORY_FLOOR, profile_id="gp_test",
            )),
            use_case="Booking confirmation & upcoming-stay support",
        )

        assert "# Scope (guardrails)" in brain._static_system
        assert "Booking confirmation & upcoming-stay support" in brain._static_system
        # Configured intents double as the allowed-topic hints.
        assert "booking_confirmation" in brain._static_system
        # And the block flows into every generation via the static prompt —
        # never only into the per-turn engine instruction.
        assert brain._scope_instruction in brain._static_system

    def test_without_profile_prompt_is_unchanged(self):
        brain = self._brain(
            guardrails=GuardrailEngine(EffectiveGuardrails(rules=MANDATORY_FLOOR)),
        )

        assert "# Scope (guardrails)" not in brain._static_system
        assert brain._scope_instruction == ""
