"""Current date/time grounding — the config-gated `# Current date and time`
runtime-context section (shared.orchestration.time_context) and its wiring
into the brain's generations and routing.

The section is computed at GENERATION time in the tenant's timezone, so
relative-date questions ("what is today's date?", "is my check-in
tomorrow?") answer against the real clock instead of a training-data guess.
Bots that have not opted in (llm_settings.time_context_enabled) are
untouched.
"""

import asyncio
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from shared.bot_config import ResolvedBotConfig
from shared.orchestration.time_context import (
    asks_current_datetime,
    time_context_section,
)
from voice_runtime.brain import ConversationBrain


class TestTimeContextSection:
    def test_renders_now_in_the_configured_timezone(self):
        fixed = datetime(2026, 8, 24, 9, 30, tzinfo=timezone.utc)
        section = time_context_section("Asia/Kolkata", now=fixed)
        assert section.startswith("\n\n# Current date and time")
        assert "2026-08-24" in section
        assert "Monday" in section
        assert "3:00 PM" in section          # 09:30 UTC = 15:00 IST
        assert "Asia/Kolkata" in section
        assert "UTC+05:30" in section
        # The grounding rule that anchors relative dates.
        assert "tomorrow" in section and "yesterday" in section

    def test_invalid_or_missing_timezone_falls_back_to_utc(self):
        fixed = datetime(2026, 8, 24, 9, 30, tzinfo=timezone.utc)
        for tz in (None, "", "Mars/Olympus"):
            section = time_context_section(tz, now=fixed)
            assert "UTC" in section
            assert "2026-08-24" in section

    def test_date_crosses_midnight_in_local_zone(self):
        # 20:00 UTC on the 24th is already the 25th in Kolkata.
        fixed = datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc)
        assert "2026-08-25" in time_context_section("Asia/Kolkata", now=fixed)

    def test_live_call_uses_the_real_clock(self):
        section = time_context_section("Asia/Kolkata")
        today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
        assert today in section


class TestAsksCurrentDatetime:
    ASKS = [
        "What is today's date?",
        "what's the date today",
        "which day is today?",
        "What day is it?",
        "what time is it right now",
        "aaj ki tarikh kya hai",
        "aaj kya date hai?",
        "aaj kaun sa din hai",
        "आज की तारीख क्या है?",
        "आज कौन सा दिन है",
        "अभी क्या समय है?",
    ]
    NOT_ASKS = [
        "when is my check-in date",          # a BOOKING date, not today's
        "my booking is for the 25th",
        "change the date of my booking",
        "call me tomorrow",
        "kal check-in hai mera",
        "",
    ]

    def test_current_datetime_questions(self):
        for text in self.ASKS:
            assert asks_current_datetime(text), text

    def test_other_date_mentions_do_not_match(self):
        for text in self.NOT_ASKS:
            assert not asks_current_datetime(text), text


# ── brain wiring ─────────────────────────────────────────────────────────────


class _RecorderStub:
    def __init__(self):
        self.events = []
        self.session_id = "s-test"
        self.usage = {"kb_searches": 0, "llm_output_tokens": 0,
                      "llm_input_tokens": 0}
        self.turns = []
        self.language = "en-IN"

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    def add_turn(self, turn):
        self.turns.append(turn)

    async def flush_event(self, kind, **data):
        self.events.append((kind, data))

    def flush_event_soon(self, kind, **data):
        self.events.append((kind, data))


class _LLMStub:
    def __init__(self, reply="Today is the twenty-fourth of August."):
        self.reply = reply
        self.systems = []

    def stream(self, history, *, system, temperature, max_tokens):
        self.systems.append(system)

        async def _gen():
            yield self.reply

        return _gen()


def make_brain(*, time_context_enabled: bool, tz="Asia/Kolkata") -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="en-IN", languages=["en-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are Test.",
        llm={"settings": {"time_context_enabled": time_context_enabled}},
        timezone=tz,
    )
    brain = ConversationBrain(
        config=config, llm=_LLMStub(), recorder=_RecorderStub(),
        finalize_grace=0.05,
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

    async def _cancel_task(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    brain.create_task = _create_task
    brain.cancel_task = _cancel_task
    return brain


class TestBrainTimeContext:
    async def test_enabled_bot_gets_a_fresh_dated_section_per_generation(self):
        brain = make_brain(time_context_enabled=True)
        await brain._handle_turn("tell me about my day please")
        system = brain._llm.systems[0]
        assert "# Current date and time" in system
        today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
        assert today in system
        assert "Asia/Kolkata" in system

    async def test_disabled_bot_prompt_is_untouched(self):
        brain = make_brain(time_context_enabled=False)
        await brain._handle_turn("tell me about my day please")
        assert "# Current date and time" not in brain._llm.systems[0]

    async def test_date_question_bypasses_knowledge_retrieval(self):
        """"What is today's date?" is KNOWLEDGE-shaped, but with time context
        enabled it must route to grounded CHAT, not tenant retrieval."""
        brain = make_brain(time_context_enabled=True)
        # Knowledge would normally claim this question shape.
        brain._router = type(brain._router)(
            intents=[], has_knowledge_bases=True,
        )
        brain._config.kb_ids = ["kb1"]
        await brain._handle_turn("What is today's date?")
        routes = [d for k, d in brain._recorder.events if k == "route_decision"]
        assert routes and routes[0]["reason"] == "time_context_question"
        assert routes[0]["route"] == "chat"

    async def test_timezone_flows_from_config(self):
        brain = make_brain(time_context_enabled=True, tz="UTC")
        await brain._handle_turn("tell me about my day please")
        system = brain._llm.systems[0]
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert today_utc in system
        assert re.search(r"\(UTC, UTC\+00:00\)", system)
