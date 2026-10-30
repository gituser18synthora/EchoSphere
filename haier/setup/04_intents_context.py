"""Stage 04 — router intents (workflow ENTRY), runtime-context schema, tenant
turn detection.

Why the bot was prompt-only: no intent carried a workflow route, so the router
could never derive the opening-affirmation entry. ``opening_acknowledgement``
now points at the workflow BY ID (never by name) and gains the short
acknowledgements from the real calls ("hmm", "ji", "han"). A second entry
intent catches first answers that already state a future plan.

Runtime-context schema lists every dialer variable (WEBHOOK_CONTRACT.md) with
a Testing-Studio payload. Turn detection: telephony silence ladder = 2
check-ins ("kya aap mujhe sun paa rahe hain?" wording is platform-owned).

Run: env/bin/python haier/setup/04_intents_context.py [--apply]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import haier_config as H  # noqa: E402
from _common import banner, check, client, summary, write  # noqa: E402

EXTRA_OPENING_SAMPLES = [
    "hmm", "hm", "hmm haan", "haan hmm", "ji", "ji ji", "han", "hanji", "haanji", "haan haan",
    "bolo", "bataiye", "batayiye", "kahiye", "haan ji boliye", "ji madam", "ji sir",
    "haan ji madam boliye", "haan bol raha hoon", "haan bol rahi hoon", "ha ji", "haa ji",
]
FUTURE_INTENT = {
    "name": "haier_future_requirement", "category": "rejection_followup",
    "description": ("The customer's FIRST answer already says the service/installation is wanted "
                    "later (after a month, after shifting, when back from out of station, kept "
                    "aside for now). Enters the rejected-WO workflow so the mandatory technician "
                    "check and the 5-day rule still run."),
    "samples": [
        "ek mahine baad karwayenge", "ek mahine baad installation hoga", "abhi nahi hogi",
        "baad mein install karana hai", "10 din baad karana hai", "das din baad karwayenge",
        "shift hone ke baad karwaunga", "out of station hoon", "main bahar hoon abhi",
        "abhi rakha hua hai", "ek month baad", "abhi nahi chahiye baad mein", "agle mahine karwayenge",
        "abhi installation nahi karwani", "baad mein karwa lenge",
    ],
    "confidenceThreshold": 0.55,
}

banner("04 intents + runtime context + turn detection")
c = client()
workflow = check(c.get(f"/bots/{H.BOT}/workflow"), "read workflow")
wf_id = workflow["id"]
print(f"     workflow {wf_id} '{workflow.get('name')}' v{workflow.get('version')}")
intents = {i["name"]: i for i in check(c.get(f"/bots/{H.BOT}/intents"), "list intents")}

opening = intents["opening_acknowledgement"]
samples = list(opening.get("samples") or [])
for s in EXTRA_OPENING_SAMPLES:
    if s not in samples:
        samples.append(s)
write(c, "PATCH", f"/intents/{opening['id']}",
      "opening_acknowledgement → routes into the workflow (workflowId) + short-ack samples",
      json_body={"workflowId": wf_id, "samples": samples,
                 "description": (opening.get("description") or "") + " Enters the rejected-WO workflow."})
if FUTURE_INTENT["name"] in intents:
    write(c, "PATCH", f"/intents/{intents[FUTURE_INTENT['name']]['id']}", "update haier_future_requirement",
          json_body={**FUTURE_INTENT, "workflowId": wf_id})
else:
    write(c, "POST", f"/bots/{H.BOT}/intents", "create haier_future_requirement (→ workflow)",
          json_body={**FUTURE_INTENT, "workflowId": wf_id})

existing_ctx = check(c.get(f"/bots/{H.BOT}/runtime-context"), "read runtime context") or {}
print(f"     current fields: {[f.get('key') for f in (existing_ctx.get('fields') or [])]}")
write(c, "PUT", f"/bots/{H.BOT}/runtime-context", "runtime-context schema (dialer variables + test payload)",
      json_body={"name": "Haier rejected-WO call context", "sourceMode": "manual",
                 "fields": H.CONTEXT_FIELDS, "allowAdditional": True, "testPayload": H.TEST_PAYLOAD,
                 "missingValuePolicy": H.MISSING_VALUE_POLICY, "domainPolicy": "generic"})

if c.headers.get("X-Login") == "tenant_admin":
    td = check(c.get("/tenant/turn-detection"), "read tenant turn detection")
    print(f"     current turn detection: mode={td.get('mode')} overrides={td.get('overrides')}")
    write(c, "PUT", "/tenant/turn-detection", "tenant turn detection (telephony silence ladder: 2 check-ins)",
          json_body=H.TURN_DETECTION)
else:
    print("\n### WRITE PUT /tenant/turn-detection — tenant turn detection (telephony silence ladder: 2 check-ins)\n"
          + __import__("json").dumps(H.TURN_DETECTION, indent=1)
          + "\n     NOT SENT: this endpoint is tenant_admin-only (super admin gets HTTP 403)."
          "\n     Re-run this stage with HAIER_ADMIN_EMAIL / HAIER_ADMIN_PASSWORD set to the existing"
          "\n     Haier tenant admin (admin@haier.com), or set the same values in Studio → Tenant settings"
          "\n     → Turn detection → Telephony → No-response: prompt 8 s, retry 8 s, 2 prompts.")
summary()
