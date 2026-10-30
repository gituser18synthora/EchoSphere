"""Stage 00 — tenant governance for Zepto (tn_04250683f1b3). SUPER ADMIN.

The tenant was onboarded as industry "logistics" with the Standard guardrail
profile. Standard carries only profanity_deescalation on top of the four
always-on mandatory guardrails — NOT enough for a delivery-partner payout
support bot: every one of its four concerns (MDND, uniform deduction,
onboarding-fee deduction, RTO) is about MONEY taken from the partner's
payout, so the bot must be hard-blocked from ever soliciting payment
credentials (card numbers / CVV / OTPs / UPI PINs) on a call — it only
COLLECTS complaint details; it never takes or refunds a payment.

What this stage does (idempotent, audited platform config — no schema change):
  1. industry stays "logistics" (already correct — asserted, never changed).
  2. guardrail profile → a dedicated "Logistics Partner Support" profile
     (code ``logistics_partner_support``) built from EXISTING enforced rule
     codes:
       - profanity_deescalation         (flag)  — irate-partner call handling
       - payment_collection_restriction (block) — the bot may never request
         card numbers, CVV, OTPs, PINs or UPI credentials
     on top of the four always-on mandatory guardrails (pii_redaction,
     secret_leakage_prevention, unsafe_tool_call_block,
     prompt_injection_protection). Enforcement keys off Guardrail.code, so no
     engine change is involved (same recipe as honasa/frankfinn stage 00).
     booking_commitment_restriction is skipped (travel-branded canned reply);
     medical/competitor rules are irrelevant to this domain.
  3. service account zepto.config@zepto.com (Tenant Admin) — API connections
     anchor their tenant from the caller, so tenant-scoped configuration
     needs a tenant-admin login (same pattern as oyo/honasa/frankfinn).

Run: env/bin/python zepto/setup/00_tenant_governance.py
"""

import sys

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
TENANT = "tn_04250683f1b3"
PROFILE_CODE = "logistics_partner_support"
PROFILE_RULES = ("profanity_deescalation", "payment_collection_restriction")
SERVICE_EMAIL = "zepto.config@zepto.com"
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

# 1. Industry must already be logistics (assert — never silently change it).
tenant = check(c.get(f"/tenants/{TENANT}"), "read tenant")
if tenant.get("industry") != "logistics":
    print(f"FAIL tenant industry is '{tenant.get('industry')}', expected "
          "'logistics' — fix the tenant before running this stage")
    sys.exit(1)
print(f"     tenant '{tenant.get('name')}' industry=logistics (correct)")

# Partners speak English, Hindi and Hinglish — the bot needs both languages
# assigned at tenant level (greeting variants validate against this list).
langs = sorted(set((tenant.get("defaultLanguages") or []) + ["en-IN", "hi-IN"]))
check(c.patch(f"/tenants/{TENANT}", json={"defaultLanguages": langs}),
      f"tenant languages -> {langs}")

# 2. Logistics Partner Support guardrail profile (reuse when it exists).
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
        "name": "Logistics Partner Support",
        "description": ("Inbound support voice bots for logistics / "
                        "quick-commerce delivery partners raising payout "
                        "deduction concerns: abuse de-escalation plus a hard "
                        "block on the bot ever requesting card numbers, CVV, "
                        "OTPs, PINs or UPI credentials (the bot only records "
                        "complaint details; payouts are settled by the "
                        "payouts team, never on a call), on top of the "
                        "mandatory platform guardrails."),
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
              else effective.get("rules", []))
)
print(f"     effective rules: {names}")

# 3. Tenant-admin service account for the configuration scripts.
r = c.post("/users", json={
    "name": "Zepto Config Service",
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
