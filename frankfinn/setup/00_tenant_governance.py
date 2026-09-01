"""Stage 00 — tenant governance for Frankfinn (tn_6553beac240d). SUPER ADMIN.

The tenant was onboarded as industry "education" with the Standard guardrail
profile. Standard carries only profanity_deescalation on top of the four
always-on mandatory guardrails — that is NOT enough for an outbound
admissions-counselling bot that talks about scholarships and course fees:
the bot must be hard-blocked from ever soliciting payment credentials
(card numbers / CVV / OTP / PIN) on a call, because the seminar it books is
free and all fee payment happens at the centre, never on the phone.

What this stage does (idempotent, audited platform config — no schema change):
  1. industry stays "education" (already correct — asserted, never changed).
  2. guardrail profile → a dedicated "Education Counselling" profile
     (code ``education_counselling``) built from EXISTING enforced rule codes:
       - profanity_deescalation         (flag)  — irate/abusive call handling
       - payment_collection_restriction (block) — the bot may never request
         card numbers, CVV, OTPs or PINs
     on top of the four always-on mandatory guardrails (pii_redaction,
     secret_leakage_prevention, unsafe_tool_call_block,
     prompt_injection_protection). Enforcement keys off Guardrail.code, so no
     engine change is involved (same recipe as honasa/setup/00).
     booking_commitment_restriction is deliberately NOT included: this bot's
     whole job is confirming seminar-seat bookings ("aapki seat confirm ho
     gayi") and that rule's canned block reply is travel-branded — it would
     misfire on the legitimate, scripted confirmation.
  3. service account frankfinn.config@frankfinn.com (Tenant Admin) — API
     connections anchor their tenant from the caller, so tenant-scoped
     configuration needs a tenant-admin login (same pattern as oyo/honasa).

Anti-hallucination (never guarantee jobs / salaries / scholarship amounts
beyond the approved script wording) is enforced downstream by the workflow's
fixed node texts, the runtime-context missing-value policy and the system
prompt — deterministic guardrails cover PII/credentials/injection/abuse,
which is exactly what this profile assigns.

Run: env/bin/python frankfinn/setup/00_tenant_governance.py
"""

import sys

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
TENANT = "tn_6553beac240d"
PROFILE_CODE = "education_counselling"
PROFILE_RULES = ("profanity_deescalation", "payment_collection_restriction")
SERVICE_EMAIL = "frankfinn.config@frankfinn.com"
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

# 1. Industry must already be education (assert — never silently change it).
tenant = check(c.get(f"/tenants/{TENANT}"), "read tenant")
if tenant.get("industry") != "education":
    print(f"FAIL tenant industry is '{tenant.get('industry')}', expected "
          "'education' — fix the tenant before running this stage")
    sys.exit(1)
print(f"     tenant '{tenant.get('name')}' industry=education (correct)")

# 2. Education Counselling guardrail profile (reuse when it already exists).
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
        "name": "Education Counselling",
        "description": ("Outbound admissions/counselling voice bots for "
                        "education institutes: abuse de-escalation plus a "
                        "hard block on the bot ever requesting card numbers, "
                        "CVV, OTPs or PINs (seminar bookings are free; fee "
                        "payment never happens on a call), on top of the "
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
              else effective.get("guardrails", []))
)
print(f"     effective rules: {names}")

# 3. Tenant-admin service account for the configuration scripts.
r = c.post("/users", json={
    "name": "Frankfinn Config Service",
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
