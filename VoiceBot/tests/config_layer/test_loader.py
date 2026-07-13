"""Tests for ConfigLoader: cache hit/miss, load by id, load by phone, errors, refresh, load_model_provider."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config_layer.exceptions import (
    ConfigValidationError,
    NoVoicebotForNumberError,
    OutsideWorkingHoursError,
    ProviderNotFoundError,
    VoicebotNotFoundError,
    VoicebotNotActiveError,
)
from config_layer.loader import ConfigLoader
from config_layer.models import VoicebotConfig


@pytest.fixture
def valid_config_doc(valid_config_dict):
    return valid_config_dict


@pytest.mark.asyncio
async def test_load_cache_hit_returns_config_without_mongodb(valid_config_dict):
    loader = ConfigLoader()
    loader._cache.get = AsyncMock(return_value=valid_config_dict)
    with patch.object(loader, "_cache", loader._cache):
        config = await loader.load("vb-1")
    assert isinstance(config, VoicebotConfig)
    assert config.voicebot_id == "vb-1"
    assert config.engine.llm_provider_id == "openai"
    loader._cache.get.assert_called_once_with("vb-1")


@pytest.mark.asyncio
async def test_load_cache_miss_loads_from_mongodb_and_sets_cache(
    valid_config_doc,
):
    mock_coll = MagicMock()
    mock_coll.find_one = AsyncMock(return_value=valid_config_doc)
    loader = ConfigLoader()
    loader._cache.get = AsyncMock(return_value=None)
    loader._cache.set = AsyncMock()
    with patch("config_layer.loader.MongoDB") as mdb:
        mdb.voicebot_configs.return_value = mock_coll
        config = await loader.load("vb-1")
    assert isinstance(config, VoicebotConfig)
    assert config.voicebot_id == "vb-1"
    mock_coll.find_one.assert_called_once_with({"voicebot_id": "vb-1"})
    loader._cache.set.assert_called_once()
    call_args = loader._cache.set.call_args[0]
    assert call_args[0] == "vb-1"
    assert call_args[1]["voicebot_id"] == "vb-1"


@pytest.mark.asyncio
async def test_load_voicebot_not_found_raises():
    loader = ConfigLoader()
    loader._cache.get = AsyncMock(return_value=None)
    mock_coll = MagicMock()
    mock_coll.find_one = AsyncMock(return_value=None)
    with patch("config_layer.loader.MongoDB") as mdb:
        mdb.voicebot_configs.return_value = mock_coll
        with pytest.raises(VoicebotNotFoundError, match="unknown-id"):
            await loader.load("unknown-id")


@pytest.mark.asyncio
async def test_load_for_incoming_call_resolves_phone_to_voicebot(
    valid_config_doc,
):
    loader = ConfigLoader()
    loader._cache.get = AsyncMock(return_value=None)
    loader._cache.set = AsyncMock()
    loader._check_availability = AsyncMock(return_value=True)
    phone_coll = MagicMock()
    phone_coll.find_one = AsyncMock(
        return_value={"phone_number": "+919876543210", "voicebot_id": "vb-1"}
    )
    voicebot_coll = MagicMock()
    voicebot_coll.find_one = AsyncMock(
        return_value={"voicebot_id": "vb-1", "status": "active"}
    )
    config_coll = MagicMock()
    config_coll.find_one = AsyncMock(return_value=valid_config_doc)
    with patch("config_layer.loader.MongoDB") as mdb:
        mdb.phone_numbers.return_value = phone_coll
        mdb.voicebots.return_value = voicebot_coll
        mdb.voicebot_configs.return_value = config_coll
        config = await loader.load_for_incoming_call("+919876543210")
    assert config.voicebot_id == "vb-1"
    phone_coll.find_one.assert_called_once_with(
        {"phone_number": "+919876543210", "status": "active"}
    )


@pytest.mark.asyncio
async def test_load_for_incoming_call_no_voicebot_raises():
    loader = ConfigLoader()
    phone_coll = MagicMock()
    phone_coll.find_one = AsyncMock(return_value=None)
    with patch("config_layer.loader.MongoDB") as mdb:
        mdb.phone_numbers.return_value = phone_coll
        with pytest.raises(NoVoicebotForNumberError, match=r"\+999"):
            await loader.load_for_incoming_call("+999")


@pytest.mark.asyncio
async def test_load_for_incoming_call_voicebot_not_active_raises(
    valid_config_doc,
):
    loader = ConfigLoader()
    loader._cache.get = AsyncMock(return_value=None)
    phone_coll = MagicMock()
    phone_coll.find_one = AsyncMock(
        return_value={"phone_number": "+919876543210", "voicebot_id": "vb-1"}
    )
    voicebot_coll = MagicMock()
    voicebot_coll.find_one = AsyncMock(
        return_value={"voicebot_id": "vb-1", "status": "paused"}
    )
    with patch("config_layer.loader.MongoDB") as mdb:
        mdb.phone_numbers.return_value = phone_coll
        mdb.voicebots.return_value = voicebot_coll
        with pytest.raises(VoicebotNotActiveError, match="paused"):
            await loader.load_for_incoming_call("+919876543210")


@pytest.mark.asyncio
async def test_load_for_incoming_call_outside_working_hours_raises(
    valid_config_doc,
):
    loader = ConfigLoader()
    loader._cache.get = AsyncMock(return_value=None)
    loader._cache.set = AsyncMock()
    loader._check_availability = AsyncMock(return_value=False)
    phone_coll = MagicMock()
    phone_coll.find_one = AsyncMock(
        return_value={"phone_number": "+919876543210", "voicebot_id": "vb-1"}
    )
    voicebot_coll = MagicMock()
    voicebot_coll.find_one = AsyncMock(
        return_value={"voicebot_id": "vb-1", "status": "active"}
    )
    config_coll = MagicMock()
    config_coll.find_one = AsyncMock(return_value=valid_config_doc)
    with patch("config_layer.loader.MongoDB") as mdb:
        mdb.phone_numbers.return_value = phone_coll
        mdb.voicebots.return_value = voicebot_coll
        mdb.voicebot_configs.return_value = config_coll
        with pytest.raises(OutsideWorkingHoursError):
            await loader.load_for_incoming_call("+919876543210")


@pytest.mark.asyncio
async def test_refresh_invalidates_then_loads(valid_config_doc):
    loader = ConfigLoader()
    loader._cache.get = AsyncMock(return_value=None)
    loader._cache.set = AsyncMock()
    loader._cache.invalidate = AsyncMock()
    mock_coll = MagicMock()
    mock_coll.find_one = AsyncMock(return_value=valid_config_doc)
    with patch("config_layer.loader.MongoDB") as mdb:
        mdb.voicebot_configs.return_value = mock_coll
        config = await loader.refresh("vb-1")
    loader._cache.invalidate.assert_called_once_with("vb-1")
    assert config.voicebot_id == "vb-1"


@pytest.mark.asyncio
async def test_load_model_provider_returns_provider():
    loader = ConfigLoader()
    provider_doc = {
        "provider_id": "openai",
        "type": "llm",
        "adapter_class": "adapters.llm.openai_adapter.OpenAILLMAdapter",
        "display_name": "OpenAI",
        "models": ["gpt-4o"],
        "min_tier": "starter",
        "is_active": True,
    }
    mock_coll = MagicMock()
    mock_coll.find_one = AsyncMock(return_value=provider_doc)
    with patch("config_layer.loader.MongoDB") as mdb:
        mdb.model_providers.return_value = mock_coll
        provider = await loader.load_model_provider("openai", "llm")
    assert provider.provider_id == "openai"
    assert provider.type == "llm"
    assert provider.display_name == "OpenAI"


@pytest.mark.asyncio
async def test_load_model_provider_not_found_raises():
    loader = ConfigLoader()
    mock_coll = MagicMock()
    mock_coll.find_one = AsyncMock(return_value=None)
    with patch("config_layer.loader.MongoDB") as mdb:
        mdb.model_providers.return_value = mock_coll
        with pytest.raises(ProviderNotFoundError, match="unknown"):
            await loader.load_model_provider("unknown", "llm")


@pytest.mark.asyncio
async def test_load_model_provider_inactive_raises():
    loader = ConfigLoader()
    mock_coll = MagicMock()
    mock_coll.find_one = AsyncMock(
        return_value={
            "provider_id": "openai",
            "type": "llm",
            "adapter_class": "adapters.llm.openai_adapter.OpenAILLMAdapter",
            "display_name": "OpenAI",
            "models": ["gpt-4o"],
            "min_tier": "starter",
            "is_active": False,
        }
    )
    with patch("config_layer.loader.MongoDB") as mdb:
        mdb.model_providers.return_value = mock_coll
        with pytest.raises(ProviderNotFoundError):
            await loader.load_model_provider("openai", "llm")
