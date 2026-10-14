"""Stage 01 — bot settings (disposition schema) + the three CRM hook connections.

Voice settings: ONLY goalPolicy and llmSettings are written (the PUT is
per-field). goalPolicy keeps ``endCall.enabled: true`` and adds the
structured disposition ``summaryFields`` — post-call-only keys, so the live
Goal Engine policy stays "derived" and the bot's talk behaviour is unchanged.
stt_model, voices and the live phone number are deliberately not touched.

Connections (tenant-scoped, placeholder .example host — never resolves, so
live calls deterministically take the api node's failure edge; the script
line is still spoken and hook_status=failed is recorded for the export):
  Haier Re-dispatch Work Order · Haier Cancel Work Order · Haier Set Do Not Call

Run: env/bin/python haier/setup/01_bot_settings_connections.py [--apply]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import haier_config as H  # noqa: E402
from _common import banner, check, client, summary, write  # noqa: E402

banner("01 bot settings + connections")
c = client()
settings = check(c.get(f"/bots/{H.BOT}/voice-settings"), "read voice settings")
llm = dict(settings.get("llmSettings") or {})
llm["time_context_enabled"] = True
print(f"     current goalPolicy={json.dumps(settings.get('goalPolicy'))}")
write(c, "PUT", f"/bots/{H.BOT}/voice-settings", "goalPolicy (summaryFields + endCall) and llmSettings",
      json_body={"goalPolicy": H.GOAL_POLICY, "llmSettings": llm})

existing = {a["name"]: a["id"] for a in check(c.get("/api-connections", params={"tenantId": H.TENANT}),
                                               "list connections")}
for conn in H.CONNECTIONS:
    body = {**conn, "botId": H.BOT}
    if conn["name"] in existing:
        write(c, "PUT", f"/api-connections/{existing[conn['name']]}", f"update connection '{conn['name']}'",
              json_body=body)
    else:
        write(c, "POST", "/api-connections", f"create connection '{conn['name']}'", json_body=body)
summary()
