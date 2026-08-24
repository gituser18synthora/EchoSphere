"""Stage: runtime behavior for the customer bot (bot 1).

- Tenant timezone → Asia/Kolkata: grounds the runtime's `# Current date and
  time` LLM context (and any future tenant-local scheduling) in IST instead
  of the UTC default.
- llm_settings.time_context_enabled → true for the customer bot only: every
  LLM generation then carries the current date/time, so "what is today's
  date?" and relative-date questions ("is my check-in tomorrow?") answer
  against the real clock. Platform default stays OFF — other bots are
  untouched.

Idempotent and rerunnable; llm_settings are merged (read-modify-write), so
existing keys like goal_engine_enabled/max_output_characters are preserved.
"""

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
BOT1 = "bot_e8cf0b05bb79"


def check(r, what):
    if r.status_code >= 300:
        raise SystemExit(f"FAIL {what}: {r.status_code} {r.text[:500]}")
    print(f"ok   {what}")
    return r.json().get("data")


c = httpx.Client(base_url=BASE, timeout=30)
token = check(c.post("/auth/login", json={"email": "oyo.config@oyo.com",
                                          "password": "Demo@2026!"}), "login")["token"]
c.headers["Authorization"] = f"Bearer {token}"

check(c.put("/tenant/profile", json={"timezone": "Asia/Kolkata"}),
      "tenant timezone Asia/Kolkata")

settings = check(c.get(f"/bots/{BOT1}/voice-settings"), "read voice settings") or {}
llm_settings = dict(settings.get("llmSettings") or {})
llm_settings["time_context_enabled"] = True
check(c.put(f"/bots/{BOT1}/voice-settings", json={"llmSettings": llm_settings}),
      "enable time context (bot 1)")
print("runtime behavior done")
