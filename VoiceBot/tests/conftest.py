"""Pytest configuration and shared fixtures."""

import pytest

pytest_plugins = ["pytest_asyncio"]


@pytest.fixture
def sample_messages():
    return [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "What is 2+2?"},
    ]


@pytest.fixture
def sample_system_prompt():
    return "You are a helpful assistant."


@pytest.fixture
def sample_pcm_audio():
    """Minimal 8kHz 16-bit mono PCM (100ms of silence)."""
    return b"\x00\x00" * 800  # 800 samples = 100ms at 8kHz
