"""The per-bot identity confirmation mode: stored next to the domain policy
(runtime_context_schemas.identity_confirmation_mode), served to the Studio,
loaded with the runtime context, and honoured by the call brain.
"""

from types import SimpleNamespace

import pytest

import shared.runtime_context as rc
from backend.routers.runtime_context import SchemaPayload, _serialize_schema
from shared.models.context_models import RuntimeContextSchema
from shared.orchestration.identity_confirmation import STANDARD, STRICT
from shared.runtime_context import build_runtime_context, load_runtime_context
from voice_runtime.identity_confirmation import identity_reask

from tests.unit.test_agentic_orchestration import (
    IDENTITY_QUESTION,
    _AgenticLLMStub,
    make_agentic_brain,
)
from tests.unit.test_brain_collection_policy import bot_replies, snapshot, turn
from tests.unit.test_identity_confirmation import GREETING, _survey_brain

BOT = SimpleNamespace(id="bot-x", name="Survey")


def _row(**overrides):
    base = dict(
        id="rcs_1", bot_id="bot-x", name="User details", source_mode="manual",
        api_connection_id=None, response_path=None, fields=[], allow_additional=True,
        test_payload=None, missing_value_policy=None, domain_policy="generic",
        identity_confirmation_mode=None, status="active",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestStoredNextToTheDomainPolicy:
    def test_the_column_exists_and_is_optional(self):
        column = RuntimeContextSchema.__table__.c.identity_confirmation_mode
        assert column.nullable

    def test_the_studio_reads_null_as_the_default(self):
        assert _serialize_schema(None, BOT)["identityConfirmationMode"] is None
        assert _serialize_schema(_row(), BOT)["identityConfirmationMode"] is None
        served = _serialize_schema(_row(identity_confirmation_mode="strict"), BOT)
        assert served["identityConfirmationMode"] == "strict"

    def test_the_save_payload_takes_the_studio_spelling(self):
        body = SchemaPayload.model_validate({"identityConfirmationMode": "standard"})
        assert body.identity_confirmation_mode == "standard"
        assert SchemaPayload.model_validate({}).identity_confirmation_mode is None


def _schema(mode):
    return {
        "id": "rcs_test", "source_mode": "manual", "fields": [],
        "allow_additional": True, "test_payload": {"customer_name": "gaurav"},
        "missing_value_policy": None, "domain_policy": "collections",
        "identity_confirmation_mode": mode, "response_path": None, "api": None,
    }


@pytest.mark.parametrize("mode", (None, STANDARD, STRICT))
async def test_a_live_call_loads_the_mode(monkeypatch, mode):
    monkeypatch.setattr(rc, "_load_schema_sync", lambda tenant, bot: _schema(mode))
    monkeypatch.setattr(rc, "_load_record_sync", lambda *a, **k: None)
    ctx = await load_runtime_context("tn-x", "bot-x")
    assert ctx.identity_confirmation_mode == mode
    assert ctx.domain_policy == "collections"


class TestTheCallHonoursTheSetting:
    async def test_a_collections_bot_set_to_standard_confirms_on_the_first_reply(self):
        runtime_context = build_runtime_context(
            tenant_id="tn-x", bot_id="bot-x", payload={"customer_name": "Ramesh Kumar"},
            payload_source="test", domain_policy="collections",
            identity_confirmation_mode=STANDARD,
        )
        brain = make_agentic_brain(
            context=snapshot(), runtime_context=runtime_context, llm=_AgenticLLMStub([]),
        )
        assert brain._policy.identity_mode == STANDARD
        await brain._say(IDENTITY_QUESTION)
        await turn(brain, "हाँ जी बोलिए।")
        assert brain._policy.verified

    async def test_a_generic_bot_set_to_strict_confirms_on_the_second_reply(self):
        runtime_context = build_runtime_context(
            tenant_id="tn-x", bot_id="bot-x",
            field_definitions=[{"key": "customer_name", "type": "string"}],
            payload={"customer_name": "gaurav"}, payload_source="test",
            identity_confirmation_mode=STRICT,
        )
        brain = _survey_brain(_AgenticLLMStub([]), runtime_context=runtime_context)
        assert brain._identity.mode == STRICT
        await brain._say(GREETING)
        await turn(brain, "हाँ जी बोलिए।")
        assert bot_replies(brain)[-1] == identity_reask(0, "hi-IN", "gaurav")
        await turn(brain, "हाँ बोलिए।")
        assert brain._identity.confirmed

    async def test_the_default_follows_the_domain_policy(self):
        generic = _survey_brain(_AgenticLLMStub([]))
        assert generic._identity.mode == STANDARD
        collections = make_agentic_brain(context=snapshot(), llm=_AgenticLLMStub([]))
        assert collections._identity.mode == STRICT == collections._policy.identity_mode
        await collections._say(IDENTITY_QUESTION)
        await turn(collections, "हाँ जी बोलिए।")
        assert bot_replies(collections)[-1] == identity_reask(0, "hi-IN", "Ramesh Kumar")
