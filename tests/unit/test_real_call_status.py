"""Studio call status follows remote hangup without dialing or using databases."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.routers import real_calls as rc


CALL_UUID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def call_status(monkeypatch):
    job = {
        "jobId": "test-job", "status": "answered", "detail": "Call answered.",
        "originationUuid": CALL_UUID, "channelUuid": CALL_UUID,
    }
    bot = SimpleNamespace(id="bot_test", tenant_id="tn_test")
    monkeypatch.setattr(rc, "_bot_checked", Mock(return_value=bot))
    monkeypatch.setattr(rc, "_job_checked", AsyncMock(return_value=job))
    conversation = Mock(return_value=None)
    monkeypatch.setattr(rc, "_conversation_for", conversation)
    monkeypatch.setattr(rc.fs, "esl_configured", lambda: True)
    client = SimpleNamespace(api=AsyncMock(return_value="true\n"), close=AsyncMock())
    factory = Mock(return_value=client)
    monkeypatch.setattr(rc.fs, "ESLClient", factory)

    async def poll():
        response = await rc.real_call_status(bot.id, job["jobId"], user=object(), db=object())
        return response["data"]

    return SimpleNamespace(
        job=job, conversation=conversation, client=client, factory=factory, poll=poll,
    )


@pytest.mark.asyncio
async def test_caller_hangup_ends_job_before_conversation_is_persisted(call_status):
    call_status.client.api.return_value = "false\n"
    result = await call_status.poll()
    assert result["status"] == "ended"
    assert result["conversation"] is None
    call_status.client.api.assert_awaited_once_with(f"uuid_exists {CALL_UUID}")
    call_status.client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_completed_conversation_ends_job_even_when_esl_is_unavailable(call_status):
    call_status.conversation.return_value = {"status": "completed", "durationSec": 12}
    result = await call_status.poll()
    assert result["status"] == "ended"
    assert result["conversation"]["durationSec"] == 12
    call_status.factory.assert_not_called()


@pytest.mark.asyncio
async def test_answered_call_stays_connected_while_channel_exists(call_status):
    result = await call_status.poll()
    assert result["status"] == "answered"
    call_status.client.api.assert_awaited_once_with(f"uuid_exists {CALL_UUID}")
    call_status.client.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["", "+OK\n", "-ERR failure\n"])
async def test_unrecognized_probe_response_does_not_end_a_call(call_status, reply):
    call_status.client.api.return_value = reply
    assert (await call_status.poll())["status"] == "answered"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    rc.ProviderError("freeswitch", "timeout", "test socket failure"),
    OSError("test socket closed"),
    TimeoutError(),
])
async def test_probe_failure_preserves_status_and_closes_socket(call_status, error):
    call_status.client.api.side_effect = error
    assert (await call_status.poll())["status"] == "answered"
    call_status.client.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["dialing", "failed", "error", "timeout", "hungup", "ended"])
async def test_ringing_and_terminal_jobs_are_not_probed(call_status, status):
    call_status.job["status"] = status
    assert (await call_status.poll())["status"] == status
    call_status.factory.assert_not_called()


@pytest.mark.asyncio
async def test_missing_credentials_do_not_end_call(call_status, monkeypatch):
    monkeypatch.setattr(rc.fs, "esl_configured", lambda: False)
    assert (await call_status.poll())["status"] == "answered"
    call_status.factory.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_channel_id_never_reaches_esl_command(call_status):
    call_status.job["channelUuid"] = "bad\nuuid_kill all"
    assert (await call_status.poll())["status"] == "answered"
    call_status.factory.assert_not_called()
