"""mPokket inbound repayment-support POC (tenant tn_22a809aecf66).

Read-only assertions against the configured POC records plus behavior checks
that exercise the same code paths a live call uses: published-config
resolution, phone-number routing, intent routing, tenant isolation of the
POC knowledge base, retrieval modes and the payment_collection workflow
wiring. Skips cleanly if the POC has not been configured in this environment.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.core.security import create_access_token
from backend.main import app
from shared.db.mysql import get_sessionmaker
from shared.models import User, VoiceBot

pytestmark = pytest.mark.integration

API = "/api/v1"
TENANT = "tn_22a809aecf66"
BOT = "bot_47e52822c803"
KB = "ks_d3f5a4a1f254"
POC_NUMBER = "+91 80 4522 1010"


def _poc_configured() -> bool:
    session = get_sessionmaker()()
    try:
        bot = session.get(VoiceBot, BOT)
        return bot is not None and not bot.is_deleted and bot.status == "published"
    finally:
        session.close()


requires_poc = pytest.mark.skipif(
    not _poc_configured(), reason="mPokket POC records not present in this environment"
)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def bearer(email: str) -> dict:
    session = get_sessionmaker()()
    try:
        user = session.execute(select(User).where(User.email == email)).scalar_one()
        token = create_access_token(
            user_id=user.id, role=user.role.code, tenant_id=user.tenant_id
        )
        return {"Authorization": f"Bearer {token}"}
    finally:
        session.close()


@pytest.fixture(scope="module")
def super_admin():
    return bearer("admin@aurexion.com")


@pytest.fixture(scope="module")
def other_tenant_admin():
    return bearer("priya.sharma@meridianhealth.com")  # tenant admin of tn-001


@pytest.fixture(scope="module")
def poc_tenant_admin():
    return bearer("admin@pokket.com")  # tenant admin of tn_22a809aecf66


def _data(response):
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


@pytest.fixture(scope="module")
def cfg():
    from shared.bot_config import resolve_bot_config

    return asyncio.run(resolve_bot_config(BOT, require_published=True, use_cache=False))


@pytest.fixture(scope="module")
def router(cfg):
    from shared.orchestration.router import TurnRouter

    return TurnRouter(intents=cfg.intents, has_knowledge_bases=bool(cfg.kb_ids))


@requires_poc
class TestPublishedConfigResolution:
    """resolve_bot_config is the exact snapshot a live call pins to Redis."""

    def test_tenant_and_publication(self, cfg):
        assert cfg.tenant_id == TENANT
        assert cfg.published is True

    def test_default_language_is_hindi(self, cfg):
        assert cfg.language == "hi-IN"

    def test_providers_match_required_matrix(self, cfg):
        assert (cfg.stt["provider"], cfg.stt["model"]) == ("sarvam", "saaras:v3")
        assert (cfg.tts["provider"], cfg.tts["model"]) == ("sarvam", "bulbul:v3")
        assert (cfg.llm["provider"], cfg.llm["model"]) == ("openai", "gpt-4o-mini")
        # Secret *references* only — never resolved keys in the snapshot.
        assert cfg.stt["api_key_reference"].startswith("env:")
        assert "KEY" in cfg.tts["api_key_reference"]

    def test_per_language_voices_and_fallback(self, cfg):
        assert cfg.tts["voice"] == "ritu"  # wire code, not the profile id
        lang_map = cfg.tts["language_map"]
        assert lang_map["hi-IN"]["voice"] == "ritu"
        assert lang_map["en-IN"]["voice"] == "aditya"
        fallback = cfg.tts["fallback"]
        assert fallback and fallback["provider"] == "elevenlabs"
        assert fallback["model"] == "eleven_flash_v2_5"

    def test_kb_and_intents_attached(self, cfg):
        assert KB in cfg.kb_ids
        routes = {i["name"]: i["route"] for i in cfg.intents}
        assert routes["make_payment"] == "workflow:payment_collection"
        assert routes["penalty_charges"] == "knowledge"
        assert routes["human_agent_request"] == "handoff"
        assert routes["already_paid_dispute"] == "handoff"

    def test_script_rules_compiled_into_system_prompt(self, cfg):
        prompt = cfg.system_prompt
        assert "Kaunse madhyam se aapka payment hoga" in prompt  # MOP question
        assert "mPokket mein samay dene ke liye dhanyavaad" in prompt  # closing
        assert "50,000" in prompt  # credit-limit rule
        assert "110+" in prompt  # write-off stage
        assert "illustrative examples only" in prompt  # no-fabrication guard

    def test_greeting_identifies_company_and_automation(self, cfg):
        assert "mPokket" in cfg.greeting
        assert "automated" in cfg.greeting.lower()

    def test_inbound_number_routes_to_poc_bot(self):
        from shared.bot_config import resolve_bot_for_phone_number

        cfg = asyncio.run(resolve_bot_for_phone_number(POC_NUMBER))
        assert (cfg.tenant_id, cfg.bot_id) == (TENANT, BOT)


@requires_poc
class TestRuntimeIntentRouting:
    """The configured intents must drive the live TurnRouter (not be
    display-only records)."""

    @pytest.mark.parametrize(
        ("utterance", "kind", "action_or_intent"),
        [
            ("mujhe abhi payment karna hai", "workflow", "payment_collection"),
            ("penalty kitni lagegi mujhe batao", "knowledge", "penalty_charges"),
            ("mera cibil score kharab hoga kya", "knowledge", "cibil_impact"),
            ("recovery notice aaya hai ghar par", "knowledge", "recovery_notice"),
            ("mujhe agent se baat karni hai", "handoff", "human_agent_request"),
            ("maine payment kar di phir bhi call aa raha hai", "handoff", "already_paid_dispute"),
        ],
    )
    def test_hindi_utterances_route(self, router, utterance, kind, action_or_intent):
        decision = router.decide(utterance)
        assert decision.kind.value == kind, (utterance, decision)
        assert action_or_intent in {decision.action, decision.intent}

    def test_payment_workflow_is_registered(self):
        from shared.orchestration.workflow_engine import _GRAPH_BUILDERS

        assert "payment_collection" in _GRAPH_BUILDERS


@requires_poc
class TestTenantIsolation:
    def test_other_tenant_cannot_read_poc_documents(self, client, other_tenant_admin):
        response = client.get(f"{API}/knowledge/{KB}/documents", headers=other_tenant_admin)
        assert response.status_code == 404

    def test_other_tenant_search_cannot_use_poc_kb(self, client, other_tenant_admin):
        response = client.post(
            f"{API}/knowledge/search-test",
            headers=other_tenant_admin,
            json={"query": "penalty charges", "kbIds": [KB]},
        )
        assert response.status_code == 404

    def test_poc_kb_not_listed_for_other_tenant(self, client, other_tenant_admin):
        body = client.get(f"{API}/knowledge?pageSize=200", headers=other_tenant_admin).json()
        ids = {row["id"] for row in body["data"]}
        assert KB not in ids


@requires_poc
class TestRetrievalModes:
    """Single-KB and tenant-wide (no kb_ids) retrieval as the POC tenant's own
    admin — same service and tenant gate the voice brain uses."""

    def _search(self, client, poc_tenant_admin, **payload):
        return _data(client.post(
            f"{API}/knowledge/search-test", headers=poc_tenant_admin, json=payload,
        ))

    def test_single_kb_hindi_query(self, client, poc_tenant_admin):
        result = self._search(
            client, poc_tenant_admin, query="penalty kitni lagegi late fee", kbIds=[KB]
        )
        assert result["kbIds"] == [KB]
        assert result["sources"], result
        assert all(s["kbId"] == KB for s in result["sources"])

    def test_tenant_wide_search_without_kb_ids(self, client, poc_tenant_admin):
        result = self._search(
            client, poc_tenant_admin, query="BHIM UPI discount cashback benefits"
        )
        assert KB in result["kbIds"]
        assert any(s["kbId"] == KB for s in result["sources"])

    def test_script_facts_are_retrievable(self, client, poc_tenant_admin):
        result = self._search(
            client, poc_tenant_admin, query="recovery notice collection team visit", kbIds=[KB]
        )
        text = " ".join(s["text"] for s in result["sources"])
        assert "recovery notice" in text.lower()


@requires_poc
class TestConfiguredRecords:
    def test_prompts_published_with_required_types(self, client, super_admin):
        prompts = _data(client.get(f"{API}/bots/{BOT}/prompts", headers=super_admin))
        by_type = {p["type"]: p for p in prompts if not p.get("isDeleted")}
        assert by_type["system"]["state"] == "published"
        assert by_type["greeting"]["state"] == "published"

    def test_entities_validate_and_mask(self, client, super_admin):
        entities = _data(client.get(f"{API}/entities?tenantId={TENANT}", headers=super_admin))
        by_name = {e["name"]: e for e in entities}
        assert by_name["registered_phone_number"]["pii"] is True
        assert by_name["registered_phone_number"]["requireConfirmation"] is True
        assert set(by_name["payment_method"]["allowedValues"]) == {"UPI", "Debit Card"}

    def test_payment_method_extraction_normalizes_synonyms(self, client, super_admin):
        entities = _data(client.get(f"{API}/entities?tenantId={TENANT}", headers=super_admin))
        entity_id = next(e["id"] for e in entities if e["name"] == "payment_method")
        result = _data(client.post(
            f"{API}/entities/{entity_id}/test", headers=super_admin,
            json={"text": "main paytm se payment kar dunga"},
        ))
        assert result["matched"] is True
        assert (result.get("value") or result.get("maskedValue")) == "UPI"

    def test_phone_number_assigned_to_tenant_and_bot(self, client, super_admin):
        numbers = _data(client.get(f"{API}/phone-numbers", headers=super_admin))
        row = next(n for n in numbers if n["number"] == POC_NUMBER)
        assert row["tenant"] == "mPokket"
        assert row["bot"] == "mPokket Repayment Support (POC)"
        assert row["status"] == "assigned"
