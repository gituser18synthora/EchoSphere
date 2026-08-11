"""Validated tool execution over tenant-configured API connections.

The LLM (or a workflow node, or the intent pipeline) may only ever *select*
a tool by name; everything that matters is enforced here, server-side:

- **scoping** — the tool must belong to the session's tenant and be visible
  to the bot (bot-scoped row or tenant-wide row). A name from another tenant
  simply does not resolve;
- **permission** — tools restricted to specific intents refuse other callers;
- **verification** — state-changing tools configured with
  ``require_confirmation`` refuse to run until the caller's identity is
  confirmed on THIS call;
- **input schema** — arguments are validated against the connection's
  ``request_schema`` (a JSON-Schema subset: properties/type/required);
- **idempotency** — a state-changing call is keyed by (session, tool,
  arguments); a replay within the TTL returns the recorded result instead of
  acting twice;
- **timeout / retry** — per-connection ``timeout_ms`` and ``retries``
  (network failures only — a 4xx is an answer, not a retryable fault);
- **credential isolation** — the ``secret://`` reference resolves to a real
  credential only inside the request frame. Results and traces carry masked
  headers/fields; the LLM sees tool *results*, never tool *credentials*.

Testing Studio passes ``mock_results`` to exercise the same pipeline with
zero external calls — validation still runs, only the HTTP hop is replaced.
"""

import asyncio
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_IDEMPOTENCY_TTL_SECONDS = 24 * 3600
_MAX_ARG_BYTES = 16_384
# Connection lookups run on every tool call — a short in-process TTL cache
# keeps the hot path at a dict hit. Staleness is bounded by the TTL only
# (an edited connection applies within 45 s); misses are not cached so a
# freshly configured tool resolves immediately.
_CONNECTION_CACHE_TTL_SECONDS = 45.0
_connection_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
_connection_cache_lock = threading.Lock()

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


@dataclass
class ToolResult:
    tool: str
    ok: bool
    status: str  # ok | denied | invalid_args | not_found | error | duplicate
    data: dict | list | None = None
    mapped: dict = field(default_factory=dict)
    error: str | None = None
    latency_ms: int = 0
    mocked: bool = False
    # Masked request/response summary, safe for traces, events and the UI.
    trace: dict = field(default_factory=dict)

    def as_event(self) -> dict:
        return {
            "tool": self.tool, "ok": self.ok, "status": self.status,
            "error": self.error, "latency_ms": self.latency_ms,
            "mocked": self.mocked,
        }


def validate_args(schema: dict | None, args: dict) -> list[str]:
    """Validate tool arguments against a request_schema subset."""
    problems: list[str] = []
    if len(json.dumps(args, default=str)) > _MAX_ARG_BYTES:
        return ["arguments exceed the size limit"]
    if not isinstance(schema, dict):
        return problems
    properties = schema.get("properties") or {}
    for name in schema.get("required") or []:
        if name not in args or args.get(name) in (None, ""):
            problems.append(f"missing required argument '{name}'")
    for name, value in args.items():
        spec = properties.get(name)
        if not isinstance(spec, dict):
            continue
        expected = spec.get("type")
        check = _TYPE_CHECKS.get(expected)
        if check and value is not None and not check(value):
            problems.append(
                f"argument '{name}' must be {expected}, got {type(value).__name__}"
            )
        allowed = spec.get("enum")
        if allowed and value not in allowed:
            problems.append(f"argument '{name}' must be one of {allowed}")
    return problems


def _mask_fields(payload, masks: set[str], keep: int = 4):
    """Deep-mask configured field names inside a JSON payload."""
    from shared.customer_context import mask_tail

    if isinstance(payload, dict):
        return {
            k: (mask_tail(str(v), keep=keep) or "•••")
            if k.lower() in masks and not isinstance(v, (dict, list))
            else _mask_fields(v, masks, keep)
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [_mask_fields(v, masks, keep) for v in payload]
    return payload


def _follow_path(body, path: str):
    node = body
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


def _load_connection_sync(tenant_id: str, bot_id: str, tool: str) -> dict | None:
    """Resolve a tool by ApiConnection id or (case-insensitive) name.

    Bot-scoped rows win over tenant-wide rows of the same name. Rows from
    other tenants are invisible by construction of the query.
    """
    from shared.db.mysql import get_sessionmaker
    from shared.models import ApiConnection

    cache_key = (tenant_id, bot_id, tool)
    now = time.monotonic()
    with _connection_cache_lock:
        cached = _connection_cache.get(cache_key)
        if cached is not None and now - cached[0] < _CONNECTION_CACHE_TTL_SECONDS:
            return dict(cached[1])  # copy — callers never touch the cached entry

    session = get_sessionmaker()()
    try:
        query = (
            session.query(ApiConnection)
            .filter(
                ApiConnection.tenant_id == tenant_id,
                ApiConnection.is_deleted.is_(False),
                (ApiConnection.bot_id == bot_id) | (ApiConnection.bot_id.is_(None)),
            )
        )
        rows = [
            r for r in query.all()
            if r.id == tool or (r.name or "").strip().lower() == tool.strip().lower()
        ]
        if not rows:
            return None
        rows.sort(key=lambda r: r.bot_id is None)  # bot-scoped first
        row = rows[0]
        connection = {
            "id": row.id, "name": row.name, "method": row.method, "url": row.url,
            "auth_type": row.auth_type, "secret_ref": row.secret_ref,
            "headers": row.headers or {}, "query_params": row.query_params or {},
            "path_params": row.path_params or {}, "body_template": row.body_template,
            "request_schema": row.request_schema,
            "success_condition": row.success_condition,
            "sensitive_masks": [str(m).lower() for m in (row.sensitive_masks or [])],
            "allowed_intents": row.allowed_intents or [],
            "allowed_workflows": row.allowed_workflows or [],
            "is_state_changing": bool(row.is_state_changing),
            "require_confirmation": bool(row.require_confirmation),
            "timeout_ms": int(row.timeout_ms or 4000),
            "retries": max(0, int(row.retries or 0)),
            "response_mapping": row.response_mapping or [],
        }
        with _connection_cache_lock:
            _connection_cache[cache_key] = (time.monotonic(), dict(connection))
        return connection
    finally:
        session.close()


class ToolExecutor:
    """One executor per process; every call is fully parameterized."""

    async def execute(
        self,
        *,
        tenant_id: str,
        bot_id: str,
        tool: str,
        args: dict | None = None,
        intent: str | None = None,
        workflow: str | None = None,
        session_id: str = "",
        customer_verified: bool = False,
        context_values: dict | None = None,
        mock_results: dict | None = None,
        guardrails=None,
    ) -> ToolResult:
        args = dict(args or {})
        started = time.monotonic()

        # Guardrail gate (mandatory unsafe_tool_call_block): once a blocking
        # guardrail fired in this turn, NO tool call may proceed — the single
        # choke point covers voice, workflow api nodes and Testing Studio.
        # Call sites that only know the session id (workflow api nodes)
        # resolve the live engine from the session registry.
        if guardrails is None and session_id:
            from shared.guardrails import session_engine

            guardrails = session_engine(session_id)
        if guardrails is not None:
            gate = guardrails.check_tool_call(intent=intent, workflow=workflow)
            if not gate.allowed:
                return ToolResult(tool=tool, ok=False, status="denied",
                                  error=gate.reason)

        connection = await asyncio.to_thread(
            _load_connection_sync, tenant_id, bot_id, tool
        )
        if connection is None:
            return ToolResult(tool=tool, ok=False, status="not_found",
                              error="No such tool is configured for this bot.")

        def _denied(error: str) -> ToolResult:
            if guardrails is not None:
                # Audit-visible under the mandatory unsafe-tool guardrail.
                guardrails.record_tool_denial(
                    intent=intent, workflow=workflow, reason=error
                )
            return ToolResult(tool=tool, ok=False, status="denied", error=error)

        # Permission: intent/workflow restrictions are a closed allow-list.
        allowed_intents = connection["allowed_intents"]
        if allowed_intents and intent and intent not in allowed_intents:
            return _denied(f"Tool is not allowed for intent '{intent}'.")
        allowed_workflows = connection["allowed_workflows"]
        if allowed_workflows and workflow and workflow not in allowed_workflows:
            return _denied(f"Tool is not allowed for workflow '{workflow}'.")

        # Development/sandbox profile rule: state-changing tools may never
        # execute for real, whatever arguments the model produced. Mocked
        # Testing Studio runs (mock_results) stay usable — nothing external
        # happens there.
        if connection["is_state_changing"] and guardrails is not None and not (
            mock_results is not None and tool in mock_results
        ):
            gate = guardrails.check_state_changing_tool(
                intent=intent, workflow=workflow
            )
            if not gate.allowed:
                return ToolResult(tool=tool, ok=False, status="denied",
                                  error=gate.reason)

        # Business rule: a state-changing tool that demands confirmation may
        # not act for an unverified caller — whoever picked up the phone is
        # not yet the customer.
        if connection["is_state_changing"] and connection["require_confirmation"] \
                and not customer_verified:
            return _denied(
                "Customer identity must be verified before this action."
            )

        problems = validate_args(connection["request_schema"], args)
        if problems:
            return ToolResult(tool=tool, ok=False, status="invalid_args",
                              error="; ".join(problems))

        masks = set(connection["sensitive_masks"])
        masked_args = _mask_fields(args, masks)

        # Mocked execution (Testing Studio): validation above still ran.
        if mock_results is not None and tool in mock_results:
            data = mock_results[tool]
            return ToolResult(
                tool=tool, ok=True, status="ok", data=data,
                mapped=self._apply_mapping(connection, data),
                latency_ms=0, mocked=True,
                trace={"request": {"args": masked_args},
                       "response": _mask_fields(data, masks)},
            )

        # Idempotency: replaying a state-changing call returns the recorded
        # result instead of acting twice.
        idem_key = None
        if connection["is_state_changing"] and session_id:
            digest = hashlib.sha256(
                json.dumps(args, sort_keys=True, default=str).encode()
            ).hexdigest()[:24]
            idem_key = f"toolidem:{session_id}:{connection['id']}:{digest}"
            previous = await self._idempotency_get(idem_key)
            if previous is not None:
                previous.update({"status": "duplicate"})
                return ToolResult(
                    tool=tool, ok=bool(previous.get("ok")), status="duplicate",
                    data=previous.get("data"), mapped=previous.get("mapped") or {},
                    latency_ms=0, trace={"request": {"args": masked_args},
                                         "idempotent_replay": True},
                )

        response, error = await self._request(connection, args, context_values or {})
        latency_ms = round((time.monotonic() - started) * 1000)

        if response is None or response["status_code"] == 0:
            # No HTTP exchange happened (SSRF policy, DNS, connect failure):
            # that can never satisfy a success condition like "status < 400".
            error = error or "tool request could not be made"
            return ToolResult(tool=tool, ok=False, status="error", error=error,
                              latency_ms=latency_ms,
                              trace={"request": {"args": masked_args}, "error": error})

        ok = self._success(connection, response["status_code"]) and not error
        data = response["body"]
        result = ToolResult(
            tool=tool, ok=ok, status="ok" if ok else "error",
            data=data, mapped=self._apply_mapping(connection, data) if ok else {},
            error=None if ok else f"HTTP {response['status_code']}",
            latency_ms=latency_ms,
            trace={
                "request": {"args": masked_args, "method": connection["method"]},
                "status_code": response["status_code"],
                "response": _mask_fields(data, masks),
            },
        )
        if idem_key is not None and ok:
            await self._idempotency_put(idem_key, {
                "ok": result.ok, "data": result.data, "mapped": result.mapped,
            })
        return result

    # ── helpers ──────────────────────────────────────────────────────────

    async def _request(
        self, connection: dict, args: dict, context_values: dict
    ) -> tuple[dict | None, str | None]:
        from shared.orchestration.placeholders import resolve_placeholders
        from shared.safe_http import fetch_json
        from shared.secrets import resolve_secret

        variables = {
            **{str(k): str(v) for k, v in context_values.items()},
            **{str(k): str(v) for k, v in args.items()},
        }

        def _fill(text) -> str:
            return resolve_placeholders(str(text), variables)

        url = _fill(connection["url"])
        for key, value in (connection["path_params"] or {}).items():
            url = url.replace("{" + key + "}", _fill(value))
        params = {k: _fill(v) for k, v in connection["query_params"].items()}
        headers = {k: _fill(v) for k, v in connection["headers"].items()}
        body = connection["body_template"]
        if isinstance(body, dict):
            body = {k: (_fill(v) if isinstance(v, str) else v) for k, v in body.items()}
            # Args not covered by the template ride along explicitly.
            for key, value in args.items():
                body.setdefault(key, value)
        elif body is None and connection["method"] not in ("GET", "DELETE"):
            body = args or None

        # The resolved credential exists only inside this frame.
        secret = resolve_secret(connection["secret_ref"])
        if secret:
            if connection["auth_type"] == "bearer":
                headers.setdefault("Authorization", f"Bearer {secret}")
            elif connection["auth_type"] == "api_key":
                headers.setdefault("X-API-Key", secret)
            elif connection["auth_type"] == "basic":
                headers.setdefault("Authorization", f"Basic {secret}")

        attempts = connection["retries"] + 1
        last_error: str | None = None
        for attempt in range(attempts):
            response = await asyncio.to_thread(
                fetch_json,
                method=connection["method"], url=url, headers=headers,
                params=params, json_body=body,
                timeout_ms=connection["timeout_ms"],
            )
            if response.status_code in (0, 502, 504) and attempt < attempts - 1:
                last_error = response.error
                await asyncio.sleep(0.15 * (attempt + 1))
                continue
            return ({"status_code": response.status_code, "body": response.body},
                    response.error)
        return None, last_error or "tool request failed"

    @staticmethod
    def _success(connection: dict, status_code: int) -> bool:
        condition = (connection["success_condition"] or "").replace(" ", "")
        if condition.startswith("status<"):
            try:
                return status_code < int(condition[7:])
            except ValueError:
                pass
        elif condition.startswith("status=="):
            try:
                return status_code == int(condition[8:])
            except ValueError:
                pass
        return 200 <= status_code < 400

    @staticmethod
    def _apply_mapping(connection: dict, data) -> dict:
        """response_mapping: [{source: "data.status", target: "payment_status"}]."""
        mapped: dict = {}
        for rule in connection["response_mapping"]:
            if not isinstance(rule, dict):
                continue
            target = rule.get("target") or rule.get("slot")
            source = rule.get("source") or rule.get("path")
            if not target or not source:
                continue
            value = _follow_path(data, str(source))
            if value is not None:
                mapped[str(target)] = value
        return mapped

    @staticmethod
    async def _idempotency_get(key: str) -> dict | None:
        try:
            from shared.db.redis import get_redis

            raw = await get_redis().get(key)
            if raw is None:
                return None
            return json.loads(raw if isinstance(raw, str) else raw.decode())
        except Exception:  # noqa: BLE001 — Redis down degrades to non-idempotent
            logger.warning("idempotency read failed for %s", key, exc_info=True)
            return None

    @staticmethod
    async def _idempotency_put(key: str, payload: dict) -> None:
        try:
            from shared.db.redis import get_redis

            await get_redis().set(
                key, json.dumps(payload, default=str), ex=_IDEMPOTENCY_TTL_SECONDS
            )
        except Exception:  # noqa: BLE001
            logger.warning("idempotency write failed for %s", key, exc_info=True)


_executor: ToolExecutor | None = None


def get_tool_executor() -> ToolExecutor:
    global _executor
    if _executor is None:
        _executor = ToolExecutor()
    return _executor
