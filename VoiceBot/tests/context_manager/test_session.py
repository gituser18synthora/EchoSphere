"""Tests for RedisSession with FakeAsyncRedis."""

import json
from datetime import datetime
from unittest.mock import patch

import pytest

from orchestrator.call_state import ActiveGoal, CallState, Turn


@pytest.fixture
def patched_redis_session(monkeypatch, fake_async_redis):
    monkeypatch.setenv("REDIS_URL", "redis://localhost/0")
    monkeypatch.setattr(
        "context_manager.session.aioredis.from_url",
        lambda *a, **k: fake_async_redis,
    )
    from context_manager.session import RedisSession

    return RedisSession(), fake_async_redis


def _call_state(**kwargs):
    defaults = dict(
        call_id="c1",
        voicebot_id="vb1",
        caller_phone="+911",
        tenant_id="t1",
        call_start_time=datetime(2025, 1, 1, 12, 0, 0),
    )
    defaults.update(kwargs)
    return CallState(**defaults)


@pytest.mark.asyncio
async def test_create_writes_session_with_correct_ttl(patched_redis_session):
    session, r = patched_redis_session
    cs = _call_state()
    await session.create(cs, max_call_duration_minutes=10)
    key = session._key("t1", "vb1", "c1")
    raw = await r.get(key)
    assert raw is not None
    data = json.loads(raw)
    assert data["call_id"] == "c1"
    ttl = await r.ttl(key)
    assert ttl > 0
    assert ttl <= 10 * 2 * 60


@pytest.mark.asyncio
async def test_get_returns_dict_after_create(patched_redis_session):
    session, _ = patched_redis_session
    cs = _call_state()
    await session.create(cs, 15)
    got = await session.get("t1", "vb1", "c1")
    assert got is not None
    assert got["voicebot_id"] == "vb1"


@pytest.mark.asyncio
async def test_get_returns_none_for_unknown_call(patched_redis_session):
    session, _ = patched_redis_session
    assert await session.get("t1", "vb1", "none") is None


@pytest.mark.asyncio
async def test_save_writes_full_turns_from_call_state(patched_redis_session):
    session, _ = patched_redis_session
    cs = _call_state()
    await session.create(cs, 10)
    cs.turns = [
        Turn(0, "user", "hi", "greeting", 0.9, 0, datetime.utcnow()),
        Turn(1, "assistant", "hello", None, None, 0, datetime.utcnow()),
    ]
    cs.turn_count = 1
    cs.sentiment_trend = "positive"
    await session.save(cs, 10)
    got = await session.get("t1", "vb1", "c1")
    assert len(got["turns"]) == 2
    assert got["turns"][0]["role"] == "user"
    assert got["turns"][0]["content"] == "hi"
    assert got["turns"][1]["role"] == "assistant"
    assert got["turn_count"] == 1
    assert got["sentiment_trend"] == "positive"


@pytest.mark.asyncio
async def test_save_updates_running_summary_fields(patched_redis_session):
    session, _ = patched_redis_session
    cs = _call_state()
    await session.create(cs, 10)
    cs.turn_count = 2
    cs.running_summary = "Summary here"
    cs.running_summary_turn = 5
    cs.turns = [
        Turn(0, "user", "a", "x", 0.8, 0, datetime.utcnow()),
        Turn(1, "assistant", "b", None, None, 0, datetime.utcnow()),
    ]
    await session.save(cs, 10)
    got = await session.get("t1", "vb1", "c1")
    assert got["running_summary"] == "Summary here"
    assert got["running_summary_turn"] == 5


@pytest.mark.asyncio
async def test_save_resets_ttl(patched_redis_session):
    session, r = patched_redis_session
    cs = _call_state()
    await session.create(cs, max_call_duration_minutes=5)
    key = session._key("t1", "vb1", "c1")
    ttl1 = await r.ttl(key)
    await session.save(cs, max_call_duration_minutes=5)
    ttl2 = await r.ttl(key)
    assert ttl2 >= ttl1 - 1


@pytest.mark.asyncio
async def test_delete_removes_key(patched_redis_session):
    session, r = patched_redis_session
    cs = _call_state()
    await session.create(cs, 10)
    await session.delete("t1", "vb1", "c1")
    assert await r.get(session._key("t1", "vb1", "c1")) is None


@pytest.mark.asyncio
async def test_active_goal_serialized_when_set(patched_redis_session):
    session, _ = patched_redis_session
    cs = _call_state()
    cs.active_goal = ActiveGoal(
        goal_name="book",
        slots={"a": 1},
        started_at_turn=0,
        paused=False,
        pause_reason=None,
    )
    await session.create(cs, 10)
    got = await session.get("t1", "vb1", "c1")
    assert got["active_goal"]["goal_name"] == "book"
    assert got["active_goal"]["slots"] == {"a": 1}


@pytest.mark.asyncio
async def test_active_goal_null_when_none(patched_redis_session):
    session, _ = patched_redis_session
    cs = _call_state()
    await session.create(cs, 10)
    got = await session.get("t1", "vb1", "c1")
    assert got["active_goal"] is None


@pytest.mark.asyncio
async def test_total_token_count_sums_turns(patched_redis_session):
    session, _ = patched_redis_session
    cs = _call_state()
    await session.create(cs, 10)
    cs.turns = [
        Turn(0, "user", "one two", None, None, 0, datetime.utcnow()),
        Turn(1, "assistant", "three four five", None, None, 0, datetime.utcnow()),
    ]
    await session.save(cs, 10)
    got = await session.get("t1", "vb1", "c1")
    u_tok = session._estimate_tokens("one two")
    b_tok = session._estimate_tokens("three four five")
    assert got["total_token_count"] == u_tok + b_tok


@pytest.mark.asyncio
async def test_system_prompt_round_trip(patched_redis_session):
    session, _ = patched_redis_session
    cs = _call_state()
    cs.system_prompt = "You are a helpful bot."
    await session.create(cs, 10)
    got = await session.get("t1", "vb1", "c1")
    assert got["system_prompt"] == "You are a helpful bot."


def test_estimate_tokens_positive_int():
    from context_manager.session import RedisSession

    s = RedisSession.__new__(RedisSession)
    assert s._estimate_tokens("hello world here") > 0


def test_redis_url_empty_uses_default_localhost():
    """Empty redis_url falls back to localhost (dev-friendly)."""
    with patch("context_manager.session.Settings") as MS:
        MS.return_value.redis_url = ""
        from context_manager.session import RedisSession

        s = RedisSession()
        assert "localhost" in s._url or "127.0.0.1" in s._url
