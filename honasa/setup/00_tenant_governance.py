"""Stage 00 — tenant governance for Honasa (tn_620d5400d462). SUPER ADMIN.

The tenant was onboarded with industry "banking" and the Finance guardrail
profile — neither describes Honasa (a D2C personal-care e-commerce brand).

What this stage does (idempotent, audited platform config — no schema change):
  1. industry  banking → ecommerce (correct classification; per platform
     rules an industry change NEVER silently touches the guardrail profile).
  2. guardrail profile → a dedicated "E-commerce Support" profile
     (code ``ecommerce_support``) built from EXISTING enforced rule codes:
       - profanity_deescalation      (flag)  — irate/abusive callers
       - payment_collection_restriction (block) — a support bot must never
         solicit card numbers / CVV / OTP / PIN on a call
     on top of the four always-on mandatory guardrails (pii_redaction,
     secret_leakage_prevention, unsafe_tool_call_block,
     prompt_injection_protection). This is rule-for-rule what the previously
     assigned Finance profile enforced, under the correct governance label —
     enforcement keys off Guardrail.code, so no engine change is involved.
     booking_commitment_restriction is deliberately NOT included: its block
     reply is travel-branded ("reservations team") and would misfire on
     legitimate, API-verified refund-status statements.
  3. service account honasa.config@honasa.com (Tenant Admin) — API
     connections anchor their tenant from the caller, so tenant-scoped
     configuration needs a tenant-admin login (same pattern as oyo.config).

Hallucination prevention is enforced downstream by the workflow response
modes (llm_grounded + validation), the runtime-context missing-value policy
and the system prompt — deterministic guardrails cover PII/credentials/
injection/abuse, which is exactly what this profile assigns.

Run: env/bin/python honasa/setup/00_tenant_governance.py
"""

import sys

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
TENANT = "tn_620d5400d462"
PROFILE_CODE = "ecommerce_support"
PROFILE_RULES = ("profanity_deescalation", "payment_collection_restriction")
SERVICE_EMAIL = "honasa.config@honasa.com"
SERVICE_PASSWORD = "Demo@2026!"


def check(r: httpx.Response, what: str):
    if r.status_code >= 300:
        print(f"FAIL {what}: {r.status_code} {r.text[:500]}")
        sys.exit(1)
    print(f"ok   {what}")
    return r.json().get("data")


c = httpx.Client(base_url=BASE, timeout=30)
token = check(c.post("/auth/login", json={
    "email": "admin@aurexion.com", "password": "Admin@2026!",
}), "super admin login")["token"]
c.headers["Authorization"] = f"Bearer {token}"

# 1. Correct the industry classification.
check(c.patch(f"/tenants/{TENANT}", json={"industry": "ecommerce"}),
      "tenant industry -> ecommerce")

# 2. E-commerce Support guardrail profile (reuse when it already exists).
profiles = check(c.get("/guardrail-profiles"), "list guardrail profiles")
profile = next((p for p in profiles if p.get("code") == PROFILE_CODE), None)
if profile is None:
    guardrails = check(c.get("/guardrails"), "list guardrails")
    by_code = {g.get("code"): g["id"] for g in guardrails}
    missing = [code for code in PROFILE_RULES if code not in by_code]
    if missing:
        print(f"FAIL guardrail codes not found: {missing}")
        sys.exit(1)
    profile = check(c.post("/guardrail-profiles", json={
        "code": PROFILE_CODE,
        "name": "E-commerce Support",
        "description": ("Customer-support voice bots for e-commerce/D2C brands: "
                        "abuse de-escalation plus a hard block on the bot ever "
                        "requesting card numbers, CVV, OTPs or PINs, on top of "
                        "the mandatory platform guardrails."),
        "guardrailIds": [by_code[code] for code in PROFILE_RULES],
    }), f"create guardrail profile '{PROFILE_CODE}'")
else:
    print(f"reuse guardrail profile '{PROFILE_CODE}' ({profile['id']})")

check(c.patch(f"/tenants/{TENANT}", json={"guardrailProfileId": profile["id"]}),
      f"assign profile '{PROFILE_CODE}' to tenant")

effective = check(c.get(f"/tenants/{TENANT}/effective-guardrails"),
                  "read effective guardrails")
names = sorted(
    (g.get("code") or g.get("name") or "?")
    for g in (effective if isinstance(effective, list)
              else effective.get("guardrails", []))
)
print(f"     effective rules: {names}")

# 3. Tenant-admin service account for the configuration scripts.
r = c.post("/users", json={
    "name": "Honasa Config Service",
    "email": SERVICE_EMAIL,
    "roleCode": "tenant_admin",
    "tenantId": TENANT,
    "password": SERVICE_PASSWORD,
})
if r.status_code == 409:
    print(f"reuse service account {SERVICE_EMAIL}")
elif r.status_code >= 300:
    print(f"FAIL create service account: {r.status_code} {r.text[:400]}")
    sys.exit(1)
else:
    print(f"ok   created service account {SERVICE_EMAIL}")

login = c.post("/auth/login", json={"email": SERVICE_EMAIL,
                                    "password": SERVICE_PASSWORD})
if login.status_code >= 300:
    print(f"FAIL service account login: {login.status_code} {login.text[:300]}")
    sys.exit(1)
print("ok   service account login verified")
print("tenant governance done")
