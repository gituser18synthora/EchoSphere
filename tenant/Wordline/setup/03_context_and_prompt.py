"""Step 03 — runtime context + system prompt follow the API-sourced data.

Runtime context ("Merchant terminal data" schema):
* ``outlet_name`` / ``bank_name`` / ``asset_type`` / ``terminal_id`` / ``mid``
  leave the *test payload*: those values now come from the merchant lookup
  during the call.  Keeping fixtures there would speak "Sharma General Store"
  on every Testing Studio run and every live call without dialer data;
* the fields stay *declared* (plus ``contact_number``, minus ``mid`` which the
  CSV does not carry) so the prompt labels them and lists them as unknown
  until the lookup fills them;
* ``customer_verified: false`` in the test payload (declared boolean) turns on
  the session-verification gate: the looked-up facts become visible to the
  LLM for follow-up questions only after the workflow maps
  ``customer_verified: true`` from a successful lookup (same pattern as the
  Honasa bot's ``identity_verified``);
* ``customer_name`` and ``docket_number`` fixtures are untouched.

System prompt: a new version whose "# CALLER DATA" section says the terminal
details are known only after the Terminal ID lookup.  Published.

    env/bin/python tenant/Wordline/setup/03_context_and_prompt.py            # dry run
    env/bin/python tenant/Wordline/setup/03_context_and_prompt.py --apply
"""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tenant.Wordline.setup.common import BOT_ID, check, client, parser, show  # noqa: E402

API_SOURCED = ("terminal_id", "outlet_name", "bank_name", "asset_type", "contact_number")
DROP_FROM_PAYLOAD = (*API_SOURCED, "mid")
DECLARED = [
    {"key": "customer_name", "type": "string",
     "label": "Merchant's name (spoken in the Terminal ID question)"},
    {"key": "docket_number", "type": "string",
     "label": "Service docket number for a battery replacement visit"},
    {"key": "customer_verified", "type": "boolean",
     "label": "Merchant verified (set by the Terminal ID lookup during the call)"},
    {"key": "terminal_id", "type": "string", "label": "Terminal ID (from the lookup)"},
    {"key": "outlet_name", "type": "string", "label": "Outlet name (from the lookup)"},
    {"key": "bank_name", "type": "string", "label": "Acquiring bank (from the lookup)"},
    {"key": "asset_type", "type": "string", "label": "Terminal type (from the lookup)"},
    {"key": "contact_number", "type": "string",
     "label": "Registered contact number (from the lookup)"},
]
MISSING_POLICY = ("Never guess a merchant detail. The Terminal ID, outlet, bank, terminal "
                  "type and contact number are known only after the flow has looked up "
                  "the Terminal ID the caller gave; before that they are unknown. If a "
                  "name, docket number or any merchant detail is absent, do not invent it.")

CALLER_DATA_HEADING = "# CALLER DATA"
CALLER_DATA_NEW = """# CALLER DATA

The call data may carry customer_name and docket_number. The merchant's terminal details —
terminal_id, outlet_name, bank_name, asset_type, contact_number — are fetched from the merchant
records only after the caller gives the Terminal ID and the flow has looked it up; before that
they are unknown, and if the lookup fails they stay unknown for the whole call. Use a value
only when the caller asks about it ("मेरा terminal किस bank का है?"). Never read out the
whole record, never invent a missing value, and never say a number the call data does not hold."""
PROMPT_NOTE = "Merchant Helpdesk — terminal details from the merchant lookup API v4"


def upgrade_schema(current: dict) -> dict:
    body = {
        "name": current.get("name") or "Merchant terminal data",
        "sourceMode": current.get("sourceMode") or "manual",
        "apiConnectionId": current.get("apiConnectionId"),
        "responsePath": current.get("responsePath"),
        "fields": copy.deepcopy(DECLARED),
        "allowAdditional": bool(current.get("allowAdditional", True)),
        "testPayload": {k: v for k, v in (current.get("testPayload") or {}).items()
                        if k not in DROP_FROM_PAYLOAD},
        "missingValuePolicy": MISSING_POLICY,
        "domainPolicy": current.get("domainPolicy") or "generic",
        "identityConfirmationMode": current.get("identityConfirmationMode"),
    }
    body["testPayload"].setdefault("customer_verified", False)
    return body


def upgrade_prompt(text: str) -> str | None:
    """Replace the CALLER DATA section; None when it is already current."""
    if "fetched from the merchant records" in text:
        return None
    pattern = re.compile(r"# CALLER DATA\n.*?(?=\n\n(?:Battery, battery looks fine|# ))", re.S)
    if not pattern.search(text):
        raise SystemExit("prompt: '# CALLER DATA' section not found — refusing to guess")
    return pattern.sub(lambda _m: CALLER_DATA_NEW, text, count=1)


def main() -> None:
    args = parser(__doc__.split("\n")[0]).parse_args()
    c = client(args.base)

    current = check(c.get(f"/bots/{BOT_ID}/runtime-context"), "read runtime context") or {}
    body = upgrade_schema(current)
    show("runtime context (new)", {k: body[k] for k in ("fields", "testPayload", "missingValuePolicy")})
    dropped = [k for k in (current.get("testPayload") or {}) if k in DROP_FROM_PAYLOAD]
    print(f"     test payload keys dropped: {dropped}")

    prompts = check(c.get(f"/bots/{BOT_ID}/prompts"), "read prompts") or []
    system = next((p for p in prompts if p.get("type") == "system"), None)
    if system is None:
        raise SystemExit("no system prompt on the bot")
    active = next((v for v in system.get("versions") or []
                   if v.get("version") == system.get("activeVersion")), None)
    text = (active or {}).get("fullPrompt") or ""
    new_text = upgrade_prompt(text)
    if new_text is None:
        print("     prompt already current (no new version)")
    else:
        print(f"     prompt {system['id']} v{system.get('activeVersion')} → new version; "
              f"CALLER DATA section replaced ({len(text)} → {len(new_text)} chars)")

    if not args.apply:
        print("dry run — re-run with --apply")
        return
    check(c.put(f"/bots/{BOT_ID}/runtime-context", json=body), "save runtime context")
    if new_text is not None:
        check(c.post(f"/prompts/{system['id']}/versions",
                     json={"promptMode": "full", "fullPrompt": new_text, "note": PROMPT_NOTE}),
              "new prompt version")
        check(c.patch(f"/prompts/{system['id']}", json={"state": "approved"}), "approve prompt")
        check(c.patch(f"/prompts/{system['id']}", json={"state": "published"}), "publish prompt")
    print("done")


if __name__ == "__main__":
    main()
