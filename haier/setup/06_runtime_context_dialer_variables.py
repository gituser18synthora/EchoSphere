"""Haier rejection bot — let the FreeSWITCH webhook supply the customer context.

``bot_450a4f7b4832`` ("Appointment booking journey", tenant tn_fb39b67f5361)
is dialled with per-call ``variables`` on the signed telephony webhook::

    {"customer_name": "Arjun", "product_name": "Split AC",
     "rejection_reason": "customer_refused_visit",
     "rejection_reason_label": "the visit was declined when our technician called"}

What this step does (dry-run by default, idempotent):

* switches the bot's runtime-context schema to source mode ``dialer``: a call
  that carries variables runs on them alone — the saved test data and any
  phone-matched record are not consulted, so a field the dialer leaves out or
  sends blank is unknown on that call instead of the test customer's value;
  a session without variables (Testing Studio, a test call placed without
  them) keeps using the saved test data exactly as before;
* declares the four webhook fields plus ``l3_tagging`` — the workflow's intro
  lines speak it as ``[l3_tagging]`` and the test data carries it, but the
  webhook does not send it yet; until it does, the placeholder is dropped from
  the spoken line. Labels and descriptions document the contract in the
  Studio; the existing ``customer_name`` declaration keeps its label;
* sets a missing-value policy for the prompt when none is configured;
* leaves the test data, domain policy, identity mode and everything else
  untouched.

Usage (a Haier tenant admin, or the platform super admin)::

    HAIER_SETUP_EMAIL=admin@haier.com HAIER_SETUP_PASSWORD=… \\
        env/bin/python haier/setup/06_runtime_context_dialer_variables.py          # dry run
    … --apply                                                                   # write
    … --base https://echosphere.edas.tech/api/v1 --apply                        # demo live

Without HAIER_SETUP_EMAIL the script logs in as the platform super admin
(ECHOSPHERE_SUPER_ADMIN_EMAIL / ECHOSPHERE_SUPER_ADMIN_PASSWORD, local
defaults). The runtime reads the schema per call (no release needed), but the
voice worker must run code that knows the ``dialer`` mode
(shared/runtime_context.py ``DIALER_SOURCE_MODE``); the API must too, or the
save is rejected with 422 — an old worker would treat an already-saved
``dialer`` schema like one without a payload source (variables below nothing,
no test data on any call), so deploy the code first.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys

BOT_ID = "bot_450a4f7b4832"
SOURCE_MODE = "dialer"

# The contract with the dialer. Order is the Studio's display order.
FIELDS = [
    {
        "key": "customer_name", "label": "Name", "type": "string",
        "description": "Customer's name as the dialer sends it; spoken in the "
                       "greeting and the identity question.",
    },
    {
        "key": "product_name", "label": "Product", "type": "string",
        "description": "Haier product the rejected request is about "
                       "(Split AC, Refrigerator, …); spoken in the intro and "
                       "the reason-specific questions.",
    },
    {
        "key": "rejection_reason", "label": "Rejection reason code", "type": "string",
        "description": "Field-team rejection code the workflow branches on: "
                       "bill_not_available, customer_refused_visit, "
                       "product_not_delivered, ecom_damaged_delivery, "
                       "refused_to_pay, unit_ok_no_defect, not_haier_product, "
                       "no_response. Any other value takes the generic intro.",
    },
    {
        "key": "rejection_reason_label", "label": "Rejection reason (plain words)",
        "type": "string",
        "description": "The recorded reason in plain words for off-script "
                       "replies (\"क्यों call किया?\"); never a code.",
    },
    {
        "key": "l3_tagging", "label": "Request type (L3 tagging)", "type": "string",
        "description": "Request type spoken in the intro (installation, "
                       "complaint, …). Not sent by the dialer today; the spoken "
                       "line drops the placeholder when it is absent.",
    },
]

MISSING_VALUE_POLICY = (
    "Use only the values provided for this call. If the customer's name is "
    "missing, do not use any name: address them as sir or ma'am. If the product "
    "is missing, say 'aapka Haier product'. If the rejection reason is missing, "
    "say only that the field team marked the request rejected, without naming a "
    "reason. Never take a name, product or reason from any other source or "
    "example."
)


def upgrade_fields(fields: list[dict]) -> list[dict]:
    """The schema's field list with the dialer contract declared (pure, idempotent).

    Fields already declared keep every key they have (label, flags …) and only
    gain a missing description; the rest of the contract is appended in order.
    Fields outside the contract are kept.
    """
    out = [copy.deepcopy(f) for f in fields]
    by_key = {f.get("key"): f for f in out}
    for spec in FIELDS:
        existing = by_key.get(spec["key"])
        if existing is None:
            out.append(copy.deepcopy(spec))
            continue
        if not existing.get("description"):
            existing["description"] = spec["description"]
    return out


def upgrade_schema(current: dict) -> dict:
    """The PUT body for the bot's runtime context (pure, idempotent)."""
    return {
        "name": current.get("name") or "User details",
        "sourceMode": SOURCE_MODE,
        "apiConnectionId": None,
        "responsePath": current.get("responsePath"),
        "fields": upgrade_fields(current.get("fields") or []),
        "allowAdditional": current.get("allowAdditional", True),
        "testPayload": current.get("testPayload"),
        "missingValuePolicy": current.get("missingValuePolicy") or MISSING_VALUE_POLICY,
        "domainPolicy": current.get("domainPolicy") or "generic",
        "identityConfirmationMode": current.get("identityConfirmationMode"),
    }


def describe(current: dict, body: dict) -> list[str]:
    """Human-readable diff between the saved schema and the PUT body."""
    lines: list[str] = []
    if current.get("sourceMode") != body["sourceMode"]:
        lines.append(f"  ~ sourceMode: {current.get('sourceMode')!r} -> {body['sourceMode']!r}")
    if (current.get("missingValuePolicy") or None) != body["missingValuePolicy"]:
        lines.append("  ~ missingValuePolicy: set")
    old = {f.get("key"): f for f in (current.get("fields") or [])}
    for field in body["fields"]:
        key = field.get("key")
        if key not in old:
            lines.append(f"  + {key}: {json.dumps(field, ensure_ascii=False)}")
        elif old[key] != field:
            changed = sorted(k for k in set(old[key]) | set(field)
                             if old[key].get(k) != field.get(k))
            lines.append(f"  ~ {key}: {', '.join(f'{k}={json.dumps(field.get(k), ensure_ascii=False)}' for k in changed)}")
    return lines or ["  (no change — already applied)"]


def _client(base: str):
    import httpx

    email = os.environ.get("HAIER_SETUP_EMAIL")
    password = os.environ.get("HAIER_SETUP_PASSWORD")
    if not email or not password:
        email = os.environ.get("ECHOSPHERE_SUPER_ADMIN_EMAIL", "admin@aurexion.com")
        password = os.environ.get("ECHOSPHERE_SUPER_ADMIN_PASSWORD", "Admin@2026!")
    c = httpx.Client(base_url=base, timeout=60)
    r = c.post("/auth/login", json={"email": email, "password": password})
    if r.status_code >= 300:
        sys.exit(f"login failed as {email}: {r.status_code} {r.text[:300]}")
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
        sys.exit(f"{args.bot} has no runtime-context schema on {args.base}; configure the test data first")
    body = upgrade_schema(current)
    print(f"{args.base} {args.bot} schema {current['id']} sourceMode={current['sourceMode']} "
          f"fields={[f.get('key') for f in current.get('fields') or []]}")
    print("\n".join(describe(current, body)))
    unchanged = (
        current.get("sourceMode") == body["sourceMode"]
        and (current.get("fields") or []) == body["fields"]
        and (current.get("missingValuePolicy") or None) == body["missingValuePolicy"]
    )
    if unchanged:
        return
    if not args.apply:
        print("\nDRY RUN — would PUT /bots/%s/runtime-context with:" % args.bot)
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return
    saved = _check(c.put(f"/bots/{args.bot}/runtime-context", json=body))
    print(f"\nAPPLIED: sourceMode={saved['sourceMode']} fields={[f['key'] for f in saved['fields']]}")


if __name__ == "__main__":
    main()
