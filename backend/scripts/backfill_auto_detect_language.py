"""Clear legacy ``auto_detect_language: false`` from multilingual bots.

Before the tri-state policy (shared/providers/stt_language_policy.py) the Voice
tab pre-filled EVERY Sarvam STT schema default into ``stt_settings`` on save —
including ``auto_detect_language: false`` — so a bot that was never touched
looks exactly like one where the user deliberately turned detection off.
Those rows keep the recognizer pinned to the bot's default language and the
call can never switch languages.

This one-off backfill removes the key (→ "follow the derived default", which
is ON for a bot with more than one effective language) ONLY where:

* the persisted value is exactly ``False`` (the old schema default), and
* the bot's effective languages (own list, else tenant defaults) number > 1.

Rows with ``true``, rows without the key, and single-language bots are left
untouched. Dry-run by default; ``--apply`` writes and invalidates the cached
bot config. Restrict to one tenant/bot with ``--tenant`` / ``--bot``.

    env/bin/python backend/scripts/backfill_auto_detect_language.py [--apply] [--tenant tn_x] [--bot bot_y]
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.getcwd())

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from shared.db.mysql import get_sessionmaker  # noqa: E402
from shared.models import BotLanguage, TenantSetting, VoiceBot, VoiceBotSetting  # noqa: E402
from shared.providers.stt_language_policy import (  # noqa: E402
    AUTO_DETECT_KEY,
    effective_languages,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--tenant", default=None)
    parser.add_argument("--bot", default=None)
    args = parser.parse_args()

    session = get_sessionmaker()()
    try:
        stmt = select(VoiceBotSetting, VoiceBot).join(VoiceBot, VoiceBot.id == VoiceBotSetting.bot_id)
        if args.tenant:
            stmt = stmt.where(VoiceBot.tenant_id == args.tenant)
        if args.bot:
            stmt = stmt.where(VoiceBot.id == args.bot)
        rows = session.execute(stmt).all()
        tenant_defaults: dict[str, list[str]] = {
            tid: list(langs or [])
            for tid, langs in session.execute(
                select(TenantSetting.tenant_id, TenantSetting.default_languages)
            ).all()
        }
        changed: list[tuple[str, str, list[str]]] = []
        for vbs, bot in rows:
            settings = vbs.stt_settings or {}
            if settings.get(AUTO_DETECT_KEY) is not False:
                continue
            own = session.scalars(
                select(BotLanguage.language_code).where(BotLanguage.bot_id == bot.id)
            ).all()
            langs = effective_languages(own, tenant_defaults.get(bot.tenant_id))
            if len(langs) <= 1:
                continue
            changed.append((bot.tenant_id, bot.id, langs))
            if args.apply:
                new_settings = dict(settings)
                new_settings.pop(AUTO_DETECT_KEY, None)
                vbs.stt_settings = new_settings
                flag_modified(vbs, "stt_settings")
        for tenant_id, bot_id, langs in changed:
            print(f"{'APPLY' if args.apply else 'DRY  '} {tenant_id} {bot_id} languages={langs}: "
                  f"drop {AUTO_DETECT_KEY}=false → derived default (on)")
        print(f"{len(changed)} bot(s) {'updated' if args.apply else 'would be updated'} "
              f"out of {len(rows)} voice_bot_settings rows")
        if args.apply and changed:
            session.commit()
            from shared.bot_config import invalidate_bot_config_sync

            for tenant_id, bot_id, _ in changed:
                invalidate_bot_config_sync(tenant_id, bot_id)
            print("cached bot configs invalidated")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
