"""Tests for ConfigCache: get, set, invalidate, TTL, publish."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config_layer.cache import (
    CONFIG_CACHE_TTL,
    CONFIG_KEY_PREFIX,
    INVALIDATION_CHANNEL,
    ConfigCache,
)


@pytest.fixture
def mock_redis():
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    redis.delete = AsyncMock()
    redis.publish = AsyncMock()
    return redis


@pytest.mark.asyncio
async def test_get_returns_none_for_missing_key(mock_redis):
    with patch("config_layer.cache.aioredis") as m:
        m.from_url.return_value = mock_redis
        cache = ConfigCache()
    result = await cache.get("unknown-id")
    assert result is None
    mock_redis.get.assert_called_once_with(f"{CONFIG_KEY_PREFIX}unknown-id")


@pytest.mark.asyncio
async def test_set_then_get_returns_identical_dict(mock_redis):
    with patch("config_layer.cache.aioredis") as m:
        m.from_url.return_value = mock_redis
        cache = ConfigCache()
    config_dict = {"voicebot_id": "vb-1", "name": "Test"}
    mock_redis.get.return_value = '{"voicebot_id": "vb-1", "name": "Test"}'
    await cache.set("vb-1", config_dict)
    mock_redis.setex.assert_called_once()
    call_args = mock_redis.setex.call_args
    assert call_args[0][0] == f"{CONFIG_KEY_PREFIX}vb-1"
    assert call_args[0][1] == CONFIG_CACHE_TTL
    result = await cache.get("vb-1")
    assert result == config_dict


@pytest.mark.asyncio
async def test_invalidate_removes_key(mock_redis):
    with patch("config_layer.cache.aioredis") as m:
        m.from_url.return_value = mock_redis
        cache = ConfigCache()
    await cache.invalidate("vb-1")
    mock_redis.delete.assert_called_once_with(f"{CONFIG_KEY_PREFIX}vb-1")
    mock_redis.get.return_value = None
    result = await cache.get("vb-1")
    assert result is None


@pytest.mark.asyncio
async def test_setex_ttl_is_300(mock_redis):
    with patch("config_layer.cache.aioredis") as m:
        m.from_url.return_value = mock_redis
        cache = ConfigCache()
    await cache.set("vb-1", {"voicebot_id": "vb-1"})
    mock_redis.setex.assert_called_once()
    assert mock_redis.setex.call_args[0][1] == 300


@pytest.mark.asyncio
async def test_publish_invalidation_sends_on_channel(mock_redis):
    with patch("config_layer.cache.aioredis") as m:
        m.from_url.return_value = mock_redis
        cache = ConfigCache()
    await cache.publish_invalidation("vb-1")
    mock_redis.publish.assert_called_once_with(INVALIDATION_CHANNEL, "vb-1")
