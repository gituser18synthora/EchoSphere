"""Jeeves Airtel bot — let the telephony webhook supply the customer details.

What this step does (dry-run by default, idempotent):

* flags ``customer_name``, ``locality`` and ``connection_status`` in the bot's
  runtime-context schema as ``sessionOverride`` — a non-blank value for the
  field in the signed FreeSWITCH webhook's ``variables`` wins over the schema's
  payload source (today the manual test JSON; a User Details API once one is
  configured). A field the dialer omits or sends blank still comes from that
  source, so ``connection_status`` keeps its API/fixture fallback;
* declares ``context_line`` (same flag) so the dialer's ``contextLine``
  variable lands on a snake_case field that greetings, workflow replies
  (``{context_line}`` / ``[Context Line]``) and ``prefillFromContext`` can read;
* leaves the source mode, test payload, policy text and every other field
  untouched.

With all four fields supplied by the dialer and ``sourceMode=api``, the runtime
skips the User Details API call for that call (``runtime_context_loaded``
reports ``api_skipped: true``). Nothing changes for other bots.

Usage (a Jeeves tenant admin; same env vars as 01_structured_summary.py)::

    JEEVES_SETUP_EMAIL=admin@jeeves.in JEEVES_SETUP_PASSWORD=… \\
        env/bin/python jeeves/setup/02_runtime_context_session_override.py          # dry run
    … --apply                                                                  # write
    … --base https://echosphere.edas.tech/api/v1 --apply                       # demo live

The runtime reads the schema per call (no release/publish needed), but the
voice worker must run code that knows the flag (shared/runtime_context.py
``SESSION_OVERRIDE_FLAG``); on an older worker the flag is inert and calls
behave exactly as before.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys

BOT_ID = "bot_44a50e8c4146"
OVERRIDE_FIELDS = ("customer_name", "locality", "connection_status")
CONTEXT_LINE_FIELD = {
    "key": "context_line",
    "label": "Context line (opening line from the dialer)",
    "type": "string",
    "sessionOverride": True,
}


def upgrade_fields(fields: list[dict]) -> list[dict]:
    """The schema's field list with the dialer flags applied (pure, idempotent)."""
    out = [copy.deepcopy(f) for f in fields]
    for field in out:
        if field.get("key") in OVERRIDE_FIELDS:
            field["sessionOverride"] = True
    if not any(f.get("key") == CONTEXT_LINE_FIELD["key"] for f in out):
        out.append(copy.deepcopy(CONTEXT_LINE_FIELD))
    return out


def describe(before: list[dict], after: list[dict]) -> list[str]:
    old = {f.get("key"): f for f in before}
    lines = []
    for field in after:
        key = field.get("key")
        if key not in old:
            lines.append(f"  + {key}: {json.dumps(field, ensure_ascii=False)}")
        elif old[key] != field:
            changed = sorted(k for k in set(old[key]) | set(field) if old[key].get(k) != field.get(k))
            lines.append(f"  ~ {key}: {', '.join(f'{k}={json.dumps(field.get(k))}' for k in changed)}")
    return lines or ["  (no change — already applied)"]


def _client(base: str):
    import httpx

    email = os.environ.get("JEEVES_SETUP_EMAIL")
    password = os.environ.get("JEEVES_SETUP_PASSWORD")
    if not email or not password:
        sys.exit("set JEEVES_SETUP_EMAIL / JEEVES_SETUP_PASSWORD (a Jeeves tenant admin)")
    c = httpx.Client(base_url=base, timeout=60)
    r = c.post("/auth/login", json={"email": email, "password": password})
    if r.status_code >= 300:
        sys.exit(f"login failed: {r.status_code} {r.text[:300]}")
    c.headers["Authorization"] = f"Bearer {r.json()['data']['token']}"
    return c


def _check(r):
    if r.status_code >= 300:
        sys.exit(f"{r.request.method} {r.request.url} → {r.status_code} {r.text[:500]}")
    return r.json()["data"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default=os.environ.get("ECHOSPHERE_API_BASE", "http://127.0.0.1:9001/api/v1"))
    parser.add_argument("--apply", action="store_true", help="write the schema (default: dry run)")
    parser.add_argument("--bot", default=BOT_ID)
    args = parser.parse_args()

    c = _client(args.base)
    current = _check(c.get(f"/bots/{args.bot}/runtime-context"))
    if not current.get("configured"):
        sys.exit(f"{args.bot} has no runtime-context schema on {args.base}; nothing to flag")
    fields = current.get("fields") or []
    upgraded = upgrade_fields(fields)
    print(f"{args.base} {args.bot} schema {current['id']} sourceMode={current['sourceMode']}")
    print("\n".join(describe(fields, upgraded)))
    if upgraded == fields:
        return
    body = {
        "name": current["name"],
        "sourceMode": current["sourceMode"],
        "apiConnectionId": current.get("apiConnectionId"),
        "responsePath": current.get("responsePath"),
        "fields": upgraded,
        "allowAdditional": current.get("allowAdditional", True),
        "testPayload": current.get("testPayload"),
        "missingValuePolicy": current.get("missingValuePolicy"),
        "domainPolicy": current.get("domainPolicy") or "generic",
    }
    if not args.apply:
        print("\nDRY RUN — would PUT /bots/%s/runtime-context with:" % args.bot)
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return
    saved = _check(c.put(f"/bots/{args.bot}/runtime-context", json=body))
    flags = {f["key"]: bool(f.get("sessionOverride")) for f in saved["fields"]}
    print(f"\nAPPLIED: fields {flags}")


if __name__ == "__main__":
    main()
