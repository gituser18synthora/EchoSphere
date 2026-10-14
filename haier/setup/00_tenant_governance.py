"""Stage 00 — tenant governance for Haier (tn_fb39b67f5361). SUPER ADMIN.

  1. Guardrail profile: a NEW Haier-only row ``haier_standard_payment_restricted``
     = profanity_deescalation + payment_collection_restriction (the bot must
     never ask for or accept payment, OTP, card, bank or UPI details — rule 7)
     on top of the always-on mandatory guardrails. The existing "Standard"
     profile is NOT modified; the new row is assigned to the Haier tenant only
     and the script verifies no other tenant references it.
  2. Tenant timezone Asia/Kolkata (grounds the "# Current date and time" block).

No service account is created: every write in stages 00–05 is done by the
platform super admin, except PUT /tenant/turn-detection (stage 04) which only a
tenant_admin may call — use the existing admin@haier.com login via
HAIER_ADMIN_EMAIL / HAIER_ADMIN_PASSWORD for that stage.

Run: env/bin/python haier/setup/00_tenant_governance.py [--apply]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import haier_config as H  # noqa: E402
from _common import APPLY, banner, check, client, summary, write  # noqa: E402

banner("00 tenant governance")
c = client(prefer_tenant_admin=False)   # super admin: profiles, tenant patch, timezone
tenant = check(c.get(f"/tenants/{H.TENANT}"), "read tenant")
print(f"     tenant '{tenant.get('name')}' industry={tenant.get('industry')} "
      f"guardrailProfileId={tenant.get('guardrailProfileId')}")

# 1. guardrail profile
profiles = check(c.get("/guardrail-profiles"), "list guardrail profiles")
profile = next((p for p in profiles if p.get("code") == H.GUARDRAIL_PROFILE_CODE), None)
guardrails = check(c.get("/guardrails"), "list guardrails")
by_code = {g.get("code"): g["id"] for g in guardrails}
missing = [code for code in H.GUARDRAIL_RULES if code not in by_code]
if missing:
    raise SystemExit(f"FAIL guardrail codes not found: {missing}")
if profile is None:
    profile = write(c, "POST", "/guardrail-profiles", f"create guardrail profile '{H.GUARDRAIL_PROFILE_CODE}'",
                    json_body={
                        "code": H.GUARDRAIL_PROFILE_CODE,
                        "name": H.GUARDRAIL_PROFILE_NAME,
                        "description": ("Haier India outbound customer-care bots (rejected work-order "
                                        "follow-up): abuse de-escalation plus a hard block on ever "
                                        "requesting payment, card, CVV, OTP, PIN, bank or UPI details "
                                        "on a call. Tenant-specific copy of Standard; Standard itself "
                                        "is untouched."),
                        "guardrailIds": [by_code[code] for code in H.GUARDRAIL_RULES],
                    })
else:
    print(f"reuse guardrail profile '{H.GUARDRAIL_PROFILE_CODE}' ({profile['id']})")
profile_id = (profile or {}).get("id") or "<new-profile-id>"
write(c, "PATCH", f"/tenants/{H.TENANT}", "assign Haier guardrail profile to the Haier tenant only",
      json_body={"guardrailProfileId": profile_id})
if APPLY:
    tenants = check(c.get("/tenants", params={"pageSize": 200}), "list tenants")
    rows = tenants if isinstance(tenants, list) else tenants.get("items", [])
    others = [t["id"] for t in rows if t.get("guardrailProfileId") == profile_id and t["id"] != H.TENANT]
    print(f"     tenants referencing the new profile besides Haier: {others or 'none'}")
    if others:
        raise SystemExit("FAIL another tenant references the Haier profile")

# 2. timezone
write(c, "PUT", f"/tenant/profile?tenantId={H.TENANT}", "tenant timezone Asia/Kolkata",
      json_body={"timezone": H.TIMEZONE})
summary()
