#!/usr/bin/env python
"""Generate (never run) the per-bot SQL that moves every bot off the OpenAI models that shut
down — gpt-5-mini on 2026-12-11; gpt-4o-mini and gpt-4.1-mini on 2027-01-14 — onto gpt-6-luna,
the catalog's replacement (see backend/seeds/provider_catalog_seed.py, "OpenAI's replacement
for gpt-4o-mini / gpt-4.1 / gpt-4.1-mini"). READ-ONLY: it issues SELECTs only and prints SQL.

Covers, per row that still carries a retiring model:
- voice_bot_settings.llm_model (the conversation / reply model) and
  llm_settings.orchestration_model (a bot-chosen decision model);
- ai_config_profiles.llm_model (tenant onboarding presets);
- the catalog rows themselves (provider_models.status -> inactive) once no bot uses them,
  which the Studio then stops offering.
Every UPDATE is guarded on the current value, so a second run changes 0 rows and a bot an
operator moved meanwhile is never overwritten. Pricing rows are left in place (history).

Usage (on the target server, from the checkout):
    env/bin/python scratchpad/prod_model_migration.py > /tmp/model_migration.sql   # inspect, then
    mysql -h <host> -u <user> -p voice_bot < /tmp/model_migration.sql
    for b in $(grep -o "bot_[0-9a-f]*" /tmp/model_migration.sql | sort -u); do redis-cli --scan --pattern "botcfg:*$b*" | xargs -r redis-cli del; done
Options: --include-drafts (default: published + in_review bots only; drafts are listed as comments),
--target <model> (default gpt-6-luna), --with-reply-options (also set reply_service_tier=fast and
reply_reasoning_effort=lowest, as the Phase 3 demo bots run; effort already defaults to lowest at
call time, the fast tier costs twice the standard rate).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())   # run from the checkout (also when copied to /tmp)

RETIRING = {
    "gpt-5-mini": "2026-12-11",
    "gpt-4o-mini": "2027-01-14",
    "gpt-4.1-mini": "2027-01-14",
}
# gpt-4.1 has no announced date in the catalog; it is listed, not migrated, unless --include-gpt41.


def q(value) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--target", default="gpt-6-luna")
    parser.add_argument("--include-drafts", action="store_true")
    parser.add_argument("--include-gpt41", action="store_true")
    parser.add_argument("--with-reply-options", action="store_true")
    args = parser.parse_args()
    from sqlalchemy import create_engine, text

    from shared.config import get_settings

    retiring = dict(RETIRING)
    if args.include_gpt41:
        retiring["gpt-4.1"] = "(no date announced)"
    models = tuple(retiring)
    engine = create_engine(get_settings().mysql_url)
    out: list[str] = []
    with engine.connect() as c:
        target_ok = c.execute(text(
            "SELECT COUNT(*) FROM provider_models WHERE provider_code='openai' AND capability='llm' "
            "AND code=:m AND status='active' AND is_deleted=0"), {"m": args.target}).scalar()
        bots = c.execute(text(
            "SELECT b.id, b.name, b.tenant_id, b.status, s.llm_model, "
            "JSON_UNQUOTE(JSON_EXTRACT(s.llm_settings, '$.orchestration_model')) AS om "
            "FROM voice_bots b JOIN voice_bot_settings s ON s.bot_id = b.id "
            "WHERE b.is_deleted = 0 AND (s.llm_model IN :models "
            "OR JSON_UNQUOTE(JSON_EXTRACT(s.llm_settings, '$.orchestration_model')) IN :models) "
            "ORDER BY b.status, s.llm_model, b.id"), {"models": models}).all()
        profiles = c.execute(text(
            "SELECT id, code, llm_model FROM ai_config_profiles WHERE is_deleted = 0 AND llm_model IN :models"
        ), {"models": models}).all()
        catalog = c.execute(text(
            "SELECT id, code, status FROM provider_models WHERE provider_code='openai' AND capability='llm' "
            "AND code IN :models AND is_deleted = 0"), {"models": models}).all()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out.append(f"-- Model migration generated {now} from this database's catalog (scratchpad/prod_model_migration.py).")
    out.append(f"-- Retiring: " + ", ".join(f"{m} ({d})" for m, d in retiring.items()) + f". Target: {args.target}.")
    if not target_ok:
        out.append(f"-- !! {args.target} is NOT an active catalog model here: run alembic upgrade head (d3f5b7c9e1a2) first. Nothing below should be applied until it is.")
    active = [b for b in bots if args.include_drafts or b.status in ("published", "in_review")]
    skipped = [b for b in bots if b not in active]
    out.append(f"-- Bots to move: {len(active)}; listed only (drafts etc.): {len(skipped)}; profiles: {len(profiles)}.")
    for b in skipped:
        out.append(f"--   not moved: {b.id} {b.name!r} ({b.tenant_id}, {b.status}) llm_model={b.llm_model} orchestration_model={b.om}")
    out.append("START TRANSACTION;")
    for b in active:
        out.append(f"-- {b.id} {b.name!r} ({b.tenant_id}, {b.status})")
        if b.llm_model in retiring:
            sets = [f"llm_model = {q(args.target)}"]
            if args.with_reply_options:
                sets.append("llm_settings = JSON_SET(COALESCE(llm_settings, JSON_OBJECT()), "
                            "'$.reply_service_tier', 'fast', '$.reply_reasoning_effort', 'lowest')")
            out.append(f"UPDATE voice_bot_settings SET {', '.join(sets)}, updated_at = NOW() "
                       f"WHERE bot_id = {q(b.id)} AND llm_model = {q(b.llm_model)};")
        if b.om in retiring:
            out.append(f"UPDATE voice_bot_settings SET llm_settings = JSON_SET(llm_settings, '$.orchestration_model', {q(args.target)}), "
                       f"updated_at = NOW() WHERE bot_id = {q(b.id)} "
                       f"AND JSON_UNQUOTE(JSON_EXTRACT(llm_settings, '$.orchestration_model')) = {q(b.om)};")
    for p in profiles:
        out.append(f"UPDATE ai_config_profiles SET llm_model = {q(args.target)}, updated_at = NOW() "
                   f"WHERE id = {q(p.id)} AND llm_model = {q(p.llm_model)};  -- profile {p.code}")
    out.append("COMMIT;")
    out.append("-- After every bot above is moved (re-run this script: 0 bots), retire the catalog rows:")
    for row in catalog:
        if row.status == "active":
            out.append(f"-- UPDATE provider_models SET status = 'inactive', updated_at = NOW() WHERE id = {q(row.id)} AND code = {q(row.code)} AND status = 'active';")
    out.append("-- Then clear the moved bots' cached configs (see the docstring) — SQL does not invalidate the Redis cache.")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
