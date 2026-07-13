"""Tests for VoiceBot config FastAPI (mocked MongoDB / Redis)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.main import app
from api.services import voicebot_service
from config_layer.db import MongoDB
from tests.config_layer.conftest import _valid_config_dict


@pytest_asyncio.fixture
async def api_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def test_flatten_for_set_nested():
    from api.services.voicebot_service import flatten_for_set

    assert flatten_for_set("goals", {"a": 1, "crm_config": {"api_key": "x"}}) == {
        "goals.a": 1,
        "goals.crm_config.api_key": "x",
    }
    assert flatten_for_set("x", {}) == {"x": {}}


@pytest.mark.asyncio
async def test_setup_get_404_when_voicebot_missing(api_client):
    m_coll = MagicMock()
    m_coll.find_one = AsyncMock(return_value=None)
    with patch.object(MongoDB, "voicebot_configs", return_value=m_coll):
        r = await api_client.get("/voicebots/vb-missing/config/setup")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_setup_put_invalidates_cache(api_client):
    base = _valid_config_dict()
    updated = {**base, "name": "Updated Name"}

    m_coll = MagicMock()
    m_coll.find_one = AsyncMock(side_effect=[base, updated])
    m_coll.find_one_and_update = AsyncMock(return_value=updated)

    body = {
        "voicebot_name": "Updated Name",
        "business_name": "Test Co",
        "primary_language": "en",
        "crm_integration_type": "none",
        "crm_credentials": {
            "crm_account_id": "",
            "api_key": "",
            "webhook_url": "",
        },
        "goals": {
            "book_appointments": True,
            "capture_leads": False,
            "answer_faqs": True,
            "route_to_human": False,
            "send_sms_followup": False,
        },
        "escalation": {
            "max_call_duration": 10,
            "fallback_action": "transfer_to_agent",
            "transfer_message": "",
        },
        "availability": {
            "phone_number_id": None,
            "enable_24x7": False,
            "working_hours_start": "09:00",
            "working_hours_end": "17:00",
            "timezone": "UTC",
        },
    }

    with (
        patch.object(MongoDB, "voicebot_configs", return_value=m_coll),
        patch.object(voicebot_service.cache, "invalidate", new=AsyncMock()) as inv,
    ):
        r = await api_client.put("/voicebots/vb-1/config/setup", json=body)

    assert r.status_code == 200
    inv.assert_awaited()
    m_coll.find_one_and_update.assert_awaited()


@pytest.mark.asyncio
async def test_extraction_add_and_delete_custom_field(api_client):
    base = _valid_config_dict()
    base["call_data_extraction"] = {
        "standard_fields": {},
        "custom_fields": [],
        "storage_destinations": [],
    }

    after_add = {
        **base,
        "call_data_extraction": {
            **base["call_data_extraction"],
            "custom_fields": [
                {
                    "field_name": "Policy",
                    "data_type": "String",
                    "extraction_method": "entity_extraction",
                    "extraction_prompt": "",
                    "required": False,
                }
            ],
        },
    }
    after_del = {
        **base,
        "call_data_extraction": {
            **base["call_data_extraction"],
            "custom_fields": [],
        },
    }

    m_coll = MagicMock()
    m_coll.find_one = AsyncMock(
        side_effect=[
            base,
            base,
            after_add,
            after_add,
            after_add,
            after_del,
        ]
    )
    m_coll.find_one_and_update = AsyncMock(
        side_effect=[after_add, after_del],
    )

    field_payload = {
        "field_name": "Policy",
        "data_type": "String",
        "extraction_method": "entity_extraction",
        "extraction_prompt": "",
        "required": False,
    }

    with (
        patch.object(MongoDB, "voicebot_configs", return_value=m_coll),
        patch.object(voicebot_service.cache, "invalidate", new=AsyncMock()),
    ):
        r1 = await api_client.post(
            "/voicebots/vb-1/config/extraction/custom-fields",
            json=field_payload,
        )
        r2 = await api_client.delete(
            "/voicebots/vb-1/config/extraction/custom-fields/Policy",
        )

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["data"]["custom_fields"][0]["field_name"] == "Policy"
    assert r2.json()["data"]["custom_fields"] == []


@pytest.mark.asyncio
async def test_actions_tool_config_and_reorder(api_client):
    base = _valid_config_dict()
    base["actions_automation"] = {
        "start_of_call": [
            {"step_key": "a", "enabled": True, "order": 0, "config": {}},
            {"step_key": "b", "enabled": True, "order": 1, "config": {}},
        ],
        "during_call_tools": {},
        "tool_configs": [],
        "end_of_call": [],
    }

    after_tool = {
        **base,
        "actions_automation": {
            **base["actions_automation"],
            "tool_configs": [
                {
                    "tool_key": "book_appointment",
                    "description_llm_trigger": "book",
                    "integration_source": "calendly",
                    "status": "active",
                    "response_on_success": "",
                    "response_on_failure": "",
                    "additional_parameters": {},
                }
            ],
        },
    }
    reordered = {
        **base,
        "actions_automation": {
            **base["actions_automation"],
            "start_of_call": [
                {"step_key": "b", "enabled": True, "order": 0, "config": {}},
                {"step_key": "a", "enabled": True, "order": 1, "config": {}},
            ],
        },
    }

    m_coll = MagicMock()
    m_coll.find_one = AsyncMock(
        side_effect=[
            base,
            base,
            base,
            base,
            reordered,
        ]
    )
    m_coll.find_one_and_update = AsyncMock(side_effect=[after_tool, reordered])

    tool_body = {
        "tool_key": "ignored",
        "description_llm_trigger": "book",
        "integration_source": "calendly",
        "status": "active",
        "response_on_success": "",
        "response_on_failure": "",
        "additional_parameters": {},
    }

    with (
        patch.object(MongoDB, "voicebot_configs", return_value=m_coll),
        patch.object(voicebot_service.cache, "invalidate", new=AsyncMock()),
    ):
        rt = await api_client.put(
            "/voicebots/vb-1/config/actions/tools/book_appointment/config",
            json=tool_body,
        )
        rr = await api_client.patch(
            "/voicebots/vb-1/config/actions/start-of-call/reorder",
            json={"step_order": ["b", "a"]},
        )

    assert rt.status_code == 200
    assert rt.json()["tool_key"] == "book_appointment"
    assert rr.status_code == 200
    steps = rr.json()["data"]["start_of_call"]
    assert [s["step_key"] for s in steps] == ["b", "a"]
