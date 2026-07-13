"""Pure unit tests for token budget message builder."""

from datetime import datetime

import pytest

from context_manager.token_budget import build_llm_messages, estimate_tokens
from orchestrator.call_state import CallState, Turn


def _cs(**kwargs):
    base = dict(
        call_id="c",
        voicebot_id="v",
        caller_phone="p",
        tenant_id="t",
        system_prompt="SYSTEM PROMPT TEXT HERE",
        turns=[],
    )
    base.update(kwargs)
    return CallState(**base)


def test_returns_system_and_current_when_no_history():
    cs = _cs()
    msgs = build_llm_messages(cs, "hello", None, 4096)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == cs.system_prompt
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "hello"


def test_omits_running_summary_when_all_turns_fit():
    """Summary is skipped when full raw history fits (no duplicate context)."""
    ts = datetime.utcnow()
    turns = [
        Turn(0, "user", "a", None, None, 0, ts),
        Turn(1, "assistant", "b", None, None, 0, ts),
    ]
    cs = _cs(turns=turns, running_summary="Should not appear in messages")
    msgs = build_llm_messages(cs, "next", None, 20000)
    blob = str(msgs)
    assert "Should not appear" not in blob
    assert "a" in blob and "b" in blob


def test_includes_earlier_summary_when_turns_do_not_fit():
    """Trim path uses EARLIER IN THIS CALL summary when present."""
    ts = datetime.utcnow()
    turns = [
        Turn(0, "user", "old message long " * 40, None, None, 0, ts),
        Turn(1, "assistant", "bot old " * 40, None, None, 0, ts),
        Turn(2, "user", "new short", None, None, 0, ts),
    ]
    cs = _cs(
        turns=turns,
        system_prompt="short",
        running_summary="Caller wants insurance.",
    )
    # Window small enough that raw turns exceed budget after system+current
    msgs = build_llm_messages(cs, "current", None, 200)
    assert any(
        m.get("role") == "system"
        and "EARLIER IN THIS CALL" in (m.get("content") or "")
        for m in msgs
    )


def test_excludes_summary_when_budget_exhausted_for_history():
    cs = _cs(
        system_prompt="x " * 5000,
        running_summary="summary " * 500,
    )
    msgs = build_llm_messages(cs, "hi", None, 50)
    assert not any("EARLIER IN THIS CALL" in str(m) for m in msgs)


def test_includes_recent_turns_when_budget_allows():
    ts = datetime.utcnow()
    turns = [
        Turn(0, "user", "old message long " * 20, None, None, 0, ts),
        Turn(1, "assistant", "bot old", None, None, 0, ts),
        Turn(2, "user", "new short", None, None, 0, ts),
    ]
    cs = _cs(turns=turns, system_prompt="short system")
    msgs = build_llm_messages(cs, "current", None, 2500)
    joined = str(msgs)
    assert "new short" in joined or "current" in joined


def test_trims_oldest_when_window_small():
    ts = datetime.utcnow()
    turns = [
        Turn(i, "user" if i % 2 == 0 else "assistant", f"turn{i} " * 30, None, None, 0, ts)
        for i in range(6)
    ]
    cs = _cs(turns=turns, system_prompt="sys")
    msgs = build_llm_messages(cs, "last", None, 800)
    blob = str(msgs)
    assert "last" in blob


def test_all_small_turns_included_chronologically():
    ts = datetime.utcnow()
    turns = [
        Turn(0, "user", "u0", None, None, 0, ts),
        Turn(1, "assistant", "a0", None, None, 0, ts),
        Turn(2, "user", "u1", None, None, 0, ts),
        Turn(3, "assistant", "a1", None, None, 0, ts),
    ]
    cs = _cs(turns=turns, system_prompt="sys")
    msgs = build_llm_messages(cs, "last", None, 10000)
    # Between system and final user: all four turn contents in order
    middle = msgs[1:-1]
    contents = [m["content"] for m in middle]
    assert contents == ["u0", "a0", "u1", "a1"]


def test_minimum_two_messages_when_budget_tiny():
    cs = _cs(system_prompt="s", turns=[])
    msgs = build_llm_messages(cs, "u", None, 10)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[-1]["role"] == "user"


def test_knowledge_injected_when_provided():
    cs = _cs()
    msgs = build_llm_messages(cs, "question", "KB fact here", 4096)
    assert msgs[-1]["role"] == "user"
    assert "Context: KB fact here" in msgs[-1]["content"]
    assert "question" in msgs[-1]["content"]


def test_knowledge_absent_when_none():
    cs = _cs()
    msgs = build_llm_messages(cs, "plain", None, 4096)
    assert msgs[-1]["content"] == "plain"


def test_messages_end_with_user():
    cs = _cs(turns=[Turn(0, "user", "a", None, None, 0, datetime.utcnow())])
    msgs = build_llm_messages(cs, "b", None, 5000)
    assert msgs[-1]["role"] == "user"


def test_messages_start_with_system():
    cs = _cs()
    msgs = build_llm_messages(cs, "x", None, 5000)
    assert msgs[0]["role"] == "system"


def test_estimate_tokens_scales_with_length():
    a = estimate_tokens("one")
    b = estimate_tokens("one two three four five")
    assert isinstance(a, int) and isinstance(b, int)
    assert b > a
