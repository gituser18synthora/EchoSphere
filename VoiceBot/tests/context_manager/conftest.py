"""Shared fixtures for context_manager tests."""

import pytest
from fakeredis import FakeAsyncRedis


@pytest.fixture
def fake_async_redis():
    return FakeAsyncRedis(encoding="utf-8", decode_responses=True)
