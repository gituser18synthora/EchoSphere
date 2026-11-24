"""Mint ONE telephony test session for the Haier bot (Redis key only, TTL'd).

Equivalent to what the FreeSWITCH webhook does after verifying a dialer call,
minus the dialer: no number is dialled, no FreeSWITCH channel exists.
call_id is deliberately None, so the runtime never sends ESL commands
(uuid_kill / hangup monitor) to the real switch for this session.

args: <variables-json>      prints: SESSION=<vs_id>
Local: env/bin/python -B haier/qa/create_session.py '{"customer_name": ...}'
Demo:  ssh echosphere-live 'cd /var/www/html/EchoSphere && env/bin/python - <json>' < haier/qa/create_session.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.getcwd())

from shared.voice_sessions import create_voice_session  # noqa: E402

TENANT = "tn_fb39b67f5361"
BOT = "bot_450a4f7b4832"


async def main() -> None:
    variables = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    variables = {str(k): str(v) for k, v in variables.items()}
    payload = await create_voice_session(
        tenant_id=TENANT, bot_id=BOT, user_id=None, channel="phone",
        caller=None, call_id=None, variables=variables,
    )
    print("SESSION=" + payload["session_id"])


if __name__ == "__main__":
    asyncio.run(main())
