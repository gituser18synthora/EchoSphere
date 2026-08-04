"""Tool executor — the backend validation ladder around tenant tools.

Everything the LLM cannot be trusted with is enforced here: tenant/bot
scoping, intent/workflow allow-lists, verification-before-action, input
schema, idempotent state changes, and masked traces. Mocked execution
(Testing Studio) must pass the same ladder with only the HTTP hop replaced.
"""

import shared.orchestration.tool_executor as tool_executor_module
from shared.orchestration.tool_executor import ToolExecutor, validate_args

CONNECTION = {
    "id": "api_1", "name": "check_payment_status", "method": "POST",
    "url": "https://lms.example/payments/status", "auth_type": "bearer",
    "secret_ref": "secret://LMS_KEY", "headers": {}, "query_params": {},
    "path_params": {}, "body_template": None,
    "request_schema": {
        "properties": {
            "loan_account": {"type": "string"},
            "payment_date": {"type": "string"},
        },
        "required": ["loan_account"],
    },
    "success_condition": "status < 400",
    "sensitive_masks": ["loan_account"],
    "allowed_intents": ["already_paid"],
    "allowed_workflows": [],
    "is_state_changing": False,
    "require_confirmation": False,
    "timeout_ms": 4000, "retries": 1,
    "response_mapping": [{"source": "data.status", "target": "payment_status"}],
}


def _patch_connection(monkeypatch, connection):
    def _load(tenant_id, bot_id, tool):
        if connection is None:
            return None
        return dict(connection)

    monkeypatch.setattr(tool_executor_module, "_load_connection_sync", _load)


class TestValidateArgs:
    def test_required_and_types(self):
        schema = CONNECTION["request_schema"]
        assert validate_args(schema, {"loan_account": "LN1"}) == []
        assert "missing required argument 'loan_account'" in \
            validate_args(schema, {})[0]
        assert "must be string" in \
            validate_args(schema, {"loan_account": 123})[0]

    def test_enum(self):
        schema = {"properties": {"mode": {"type": "string", "enum": ["full", "partial"]}}}
        assert validate_args(schema, {"mode": "full"}) == []
        assert validate_args(schema, {"mode": "other"})


class TestValidationLadder:
    async def test_unknown_tool_is_not_found(self, monkeypatch):
        _patch_connection(monkeypatch, None)
        result = await ToolExecutor().execute(
            tenant_id="tn-a", bot_id="b", tool="ghost", args={},
        )
        assert result.status == "not_found" and result.ok is False

    async def test_intent_allow_list_enforced(self, monkeypatch):
        _patch_connection(monkeypatch, CONNECTION)
        result = await ToolExecutor().execute(
            tenant_id="tn-a", bot_id="b", tool="check_payment_status",
            args={"loan_account": "LN1"}, intent="book_appointment",
        )
        assert result.status == "denied"
        assert "not allowed for intent" in result.error

    async def test_verification_required_for_state_change(self, monkeypatch):
        connection = {**CONNECTION, "is_state_changing": True,
                      "require_confirmation": True, "allowed_intents": []}
        _patch_connection(monkeypatch, connection)
        executor = ToolExecutor()
        denied = await executor.execute(
            tenant_id="tn-a", bot_id="b", tool="check_payment_status",
            args={"loan_account": "LN1"}, customer_verified=False,
        )
        assert denied.status == "denied"
        assert "verified" in denied.error
        # Verified caller passes the gate (and hits the mock, not HTTP).
        allowed = await executor.execute(
            tenant_id="tn-a", bot_id="b", tool="check_payment_status",
            args={"loan_account": "LN1"}, customer_verified=True,
            mock_results={"check_payment_status": {"data": {"status": "completed"}}},
        )
        assert allowed.ok is True and allowed.mocked is True

    async def test_invalid_args_rejected_before_any_call(self, monkeypatch):
        _patch_connection(monkeypatch, CONNECTION)
        result = await ToolExecutor().execute(
            tenant_id="tn-a", bot_id="b", tool="check_payment_status",
            args={"payment_date": "kal"}, intent="already_paid",
            mock_results={"check_payment_status": {"ok": True}},
        )
        assert result.status == "invalid_args"


class TestMockedExecution:
    async def test_mock_result_flows_through_mapping_and_masking(self, monkeypatch):
        _patch_connection(monkeypatch, CONNECTION)
        result = await ToolExecutor().execute(
            tenant_id="tn-a", bot_id="b", tool="check_payment_status",
            args={"loan_account": "LN00123456"}, intent="already_paid",
            mock_results={"check_payment_status": {
                "data": {"status": "completed"}, "loan_account": "LN00123456",
            }},
        )
        assert result.ok and result.mocked
        assert result.mapped == {"payment_status": "completed"}
        # Sensitive fields masked in the trace, both directions.
        assert result.trace["request"]["args"]["loan_account"] != "LN00123456"
        assert result.trace["response"]["loan_account"] != "LN00123456"

    async def test_unmocked_tool_in_mock_mode_still_validates(self, monkeypatch):
        _patch_connection(monkeypatch, CONNECTION)
        # mock_results present but not for this tool → real execution path;
        # SSRF policy blocks the fake host, so it fails safely.
        result = await ToolExecutor().execute(
            tenant_id="tn-a", bot_id="b", tool="check_payment_status",
            args={"loan_account": "LN1"}, intent="already_paid",
            mock_results={"other_tool": {}},
        )
        assert result.ok is False


class TestIdempotency:
    async def test_state_changing_replay_returns_recorded_result(self, monkeypatch):
        connection = {**CONNECTION, "is_state_changing": True,
                      "allowed_intents": [], "require_confirmation": False}
        _patch_connection(monkeypatch, connection)
        stored: dict = {}

        async def fake_get(key):
            return stored.get(key)

        async def fake_put(key, payload):
            stored[key] = payload

        monkeypatch.setattr(ToolExecutor, "_idempotency_get",
                            staticmethod(fake_get))
        monkeypatch.setattr(ToolExecutor, "_idempotency_put",
                            staticmethod(fake_put))

        calls = {"n": 0}

        async def fake_request(self, conn, args, ctx):
            calls["n"] += 1
            return {"status_code": 200, "body": {"data": {"status": "completed"}}}, None

        monkeypatch.setattr(ToolExecutor, "_request", fake_request)
        executor = ToolExecutor()
        first = await executor.execute(
            tenant_id="tn-a", bot_id="b", tool="check_payment_status",
            args={"loan_account": "LN1"}, session_id="sess-1",
        )
        assert first.ok and calls["n"] == 1
        replay = await executor.execute(
            tenant_id="tn-a", bot_id="b", tool="check_payment_status",
            args={"loan_account": "LN1"}, session_id="sess-1",
        )
        assert replay.status == "duplicate"
        assert calls["n"] == 1  # the action ran exactly once
        assert replay.mapped == {"payment_status": "completed"}

    async def test_different_args_are_not_deduplicated(self, monkeypatch):
        connection = {**CONNECTION, "is_state_changing": True,
                      "allowed_intents": []}
        _patch_connection(monkeypatch, connection)
        stored: dict = {}
        monkeypatch.setattr(ToolExecutor, "_idempotency_get",
                            staticmethod(lambda key: _async(stored.get(key))))
        monkeypatch.setattr(ToolExecutor, "_idempotency_put",
                            staticmethod(lambda key, payload: _async(stored.__setitem__(key, payload))))
        calls = {"n": 0}

        async def fake_request(self, conn, args, ctx):
            calls["n"] += 1
            return {"status_code": 200, "body": {}}, None

        monkeypatch.setattr(ToolExecutor, "_request", fake_request)
        executor = ToolExecutor()
        await executor.execute(tenant_id="t", bot_id="b", tool="x",
                               args={"loan_account": "LN1"}, session_id="s")
        await executor.execute(tenant_id="t", bot_id="b", tool="x",
                               args={"loan_account": "LN2"}, session_id="s")
        assert calls["n"] == 2


async def _async(value):
    return value


class TestSecrets:
    async def test_resolved_secret_never_in_result_or_trace(self, monkeypatch):
        monkeypatch.setenv("LMS_KEY", "super-secret-token")
        _patch_connection(monkeypatch, CONNECTION)
        captured = {}

        def fake_fetch_json(*, method, url, headers, params, json_body, timeout_ms):
            captured["headers"] = headers

            class _Resp:
                status_code = 200
                ok = True
                latency_ms = 5
                body = {"data": {"status": "completed"}}
                error = None
                truncated = False

            return _Resp()

        import shared.safe_http as safe_http_module

        monkeypatch.setattr(safe_http_module, "fetch_json", fake_fetch_json)
        result = await ToolExecutor().execute(
            tenant_id="tn-a", bot_id="b", tool="check_payment_status",
            args={"loan_account": "LN1"}, intent="already_paid",
        )
        # The credential reached the wire…
        assert captured["headers"]["Authorization"] == "Bearer super-secret-token"
        # …and exists nowhere in what the caller (and the LLM) can see.
        assert "super-secret-token" not in str(result.trace)
        assert "super-secret-token" not in str(result.data)
