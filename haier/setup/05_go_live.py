"""Stage 05 — readiness recompute + publish (bot already published; this
re-publishes so live calls pick up the new prompt/workflow revision).

The voice channel (+918047133633 · freeswitch) and the live phone_numbers row
are NOT touched. Platform test scenarios (r7) are not created here — the
regression evidence is haier/tests (offline replay + simulate suite).

Run: env/bin/python haier/setup/05_go_live.py [--apply]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import haier_config as H  # noqa: E402
from _common import banner, check, client, summary, write  # noqa: E402

banner("05 readiness + publish")
c = client()
bot = check(c.get(f"/bots/{H.BOT}"), "read bot")
print(f"     {bot.get('name')} status={bot.get('status')} version={bot.get('version')}")
write(c, "POST", f"/bots/{H.BOT}/readiness/recompute", "recompute readiness r1..r7")
write(c, "PATCH", f"/bots/{H.BOT}", "bot status → published (refresh)", json_body={"status": "published"})
summary()
