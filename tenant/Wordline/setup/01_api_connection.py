"""Step 01 — the "Wordline Merchant Lookup" API connection (bot-scoped).

Creates or updates the ``api_connections`` row the workflow's ``api`` nodes
call.  Dry run by default; ``--apply`` writes through the platform API.

    env/bin/python tenant/Wordline/setup/01_api_connection.py            # dry run
    env/bin/python tenant/Wordline/setup/01_api_connection.py --apply    # write

Why these settings:
* POST ``/merchants/lookup`` — the engine posts every scalar workflow slot,
  the service picks the Terminal ID (retry answer first); see api/main.py.
* ``successCondition: status<400`` + ``responseSchema`` — the service answers
  a hit *and* a miss with 200 (``lookup_status`` ``found`` / ``not_found``; a
  miss maps nothing and the workflow's ``merchant_found`` condition routes
  it).  The response schema runs on every accepted status and requires
  ``lookup_status`` ∈ {``found``, ``not_found``}, so a foreign 200 body, a
  401/403 (``unauthorized``), 422 (``no_identifier``), 429 or any non-JSON
  body fails validation and takes the failure edge, like transport errors,
  timeouts and 5xx.  (A miss used to be 404 with ``status<500``; Studio's
  "Send test request" and the tenant dashboard treat every non-2xx as a
  failing endpoint, and the test can only send the fixed sample value
  ``+10000000000``, so each click marked the healthy service "failing".)
* ``secret://wordline-merchant-api-key`` → env ``WORDLINE_MERCHANT_API_KEY``
  on the voice worker / API process (sent as ``X-API-Key``).
* ``timeoutMs 2500, retries 1`` — the lookup is a local file read; a hung
  service costs the caller at most ~5 s before the unavailable branch.
* ``sensitiveMasks`` keep the contact number out of traces and events.
* ``responseMapping`` targets are the slot names the read-back nodes speak;
  ``customer_verified`` makes the looked-up facts available to the LLM for
  follow-up questions once the merchant is found.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tenant.Wordline.setup.common import (  # noqa: E402
    BOT_ID, MERCHANT_API_BASE, TENANT_ID, check, client, parser, show)
from tenant.Wordline.setup.workflow_upgrade import CONNECTION_NAME  # noqa: E402

RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["lookup_status"],
    "properties": {"lookup_status": {"type": "string", "enum": ["found", "not_found"]}},
}

CONNECTION = {
    "name": CONNECTION_NAME,
    "description": ("Looks up a Wordline merchant terminal by the Terminal ID the "
                    "caller dictates (or a registered contact number) in the tenant "
                    "merchant data API (tenant/Wordline/api, CSV-sourced). A miss is "
                    "HTTP 200 lookup_status=not_found with no mapped fields."),
    "method": "POST",
    "url": f"{MERCHANT_API_BASE}/merchants/lookup",
    "authType": "api_key",
    "secretRef": "secret://wordline-merchant-api-key",
    "headers": {"Accept": "application/json"},
    # The engine posts the whole scalar slot state on top of this template and
    # the service picks the Terminal ID out of it.  Workflow slot names are not
    # allowed template variables on connection rows, so the one template key
    # exists for the Studio "Test" button: ``{{customer_phone}}`` is an allowed
    # test variable (Studio fills it with the sample ``+10000000000``, which
    # matches no terminal → a 200 ``not_found`` → "OK"; the API's own --apply
    # test below sends a real Terminal ID) and is never a live call-context
    # key, so on real calls the literal placeholder is sent and ignored.
    "bodyTemplate": {"tid": "{{customer_phone}}"},
    "successCondition": "status<400",
    "responseSchema": RESPONSE_SCHEMA,
    "successMessage": "Merchant lookup completed.",
    "failureMessage": "Merchant data API unavailable.",
    "sensitiveMasks": ["contact_number", "contact_number_masked"],
    "isStateChanging": False,
    "requireConfirmation": False,
    "timeoutMs": 2500,
    "retries": 1,
    "responseMapping": [
        {"source": "found", "target": "merchant_found"},
        {"source": "verified", "target": "customer_verified"},
        {"source": "matched_by", "target": "merchant_matched_by"},
        {"source": "terminal_id", "target": "terminal_id"},
        {"source": "outlet_name", "target": "outlet_name"},
        {"source": "bank_name", "target": "bank_name"},
        {"source": "asset_type", "target": "asset_type"},
        {"source": "contact_number", "target": "contact_number"},
    ],
    "botId": BOT_ID,
}


def main() -> None:
    args = parser(__doc__.split("\n")[0]).parse_args()
    c = client(args.base)
    rows = check(c.get("/api-connections", params={"tenantId": TENANT_ID}), "list connections") or []
    existing = next((r for r in rows if (r.get("name") or "").strip().lower()
                     == CONNECTION_NAME.lower()), None)
    show("connection payload", {**CONNECTION, "tenantId": TENANT_ID})
    if not args.apply:
        print(f"dry run — would {'update ' + existing['id'] if existing else 'create'}; "
              "re-run with --apply")
        return
    if existing:
        row = check(c.patch(f"/api-connections/{existing['id']}",
                            json={k: v for k, v in CONNECTION.items() if k != "name"}),
                    f"update connection {existing['id']}")
    else:
        row = check(c.post("/api-connections", json={**CONNECTION, "tenantId": TENANT_ID}),
                    "create connection")
    print("connection id:", row["id"])
    probe = check(c.post(f"/api-connections/{row['id']}/test",
                         json={"testValues": {"customer_phone": "91379784"}}),
                  "connection test (Terminal ID 91379784 via test value customer_phone)")
    print(f"     ok={probe.get('ok')} status={probe.get('status')} "
          f"latency={probe.get('latencyMs')}ms body={str(probe.get('body'))[:160]}")


if __name__ == "__main__":
    main()
