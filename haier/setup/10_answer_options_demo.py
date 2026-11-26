"""Save the answer-options workflow revision on the DEMO bot and release it.

For the person who deploys DEMO. Dry-run by default: it reads the DEMO bot,
prints the two writes and sends nothing. ``--apply`` writes through DEMO's own
API only (the destination is fixed below): the next workflow revision and one
release that pins it. Prompts, greeting and summary policy are not touched;
no server file is copied and no service is restarted.

    ECHOSPHERE_SUPER_ADMIN_EMAIL=... ECHOSPHERE_SUPER_ADMIN_PASSWORD=... \
        env/bin/python -B haier/setup/10_answer_options_demo.py           # dry-run
    ... env/bin/python -B haier/setup/10_answer_options_demo.py --apply

It signs in as DEMO's platform super admin (the two variables above; a Haier
tenant-admin login is not used): publishing a release of this bot needs the
checklist override, which the API grants to a super admin only (the bot has no
knowledge base and no Studio scenarios).

Order: deploy the code first and restart the DEMO services, then run this.
The revision is safe on a runtime that does not know ``answerOptions`` (such a
runtime ignores the new keys), but it only takes effect on the matching one.

Roll back: roll the CODE back and restart. The previous runtime ignores
``answerOptions``, ``closingFollowups`` and ``doNotCallNode``, so the flow
behaves as it did before (``behavior.max_node_steps`` stays in force; it only
lifts the per-turn node budget). Do NOT use the Studio's Rollback button for
this: it marks the bot ``rolled_back``, and the runtime refuses the calls of a
bot that is not ``published`` until a release is published again. To return to
the previous workflow content while keeping the new code, save that content as
a new revision and publish a new release (every revision stays in the
database).

The script refuses a DEMO workflow that is not the reviewed one (it compares
what the flow does, not where its nodes sit on the canvas).
"""
from __future__ import annotations

import os
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

DEMO = "https://echosphere.edas.tech/api/v1"
os.environ["ECHOSPHERE_API"] = DEMO

import _common  # noqa: E402
from haier.setup.haier_answer_options_publish import publish  # noqa: E402


def main() -> None:
    if _common.BASE.rstrip("/") != DEMO:
        raise SystemExit("Refusing an unexpected API destination before authentication")
    _common.banner("Haier answer options: DEMO workflow revision + release")
    with closing(_common.client(prefer_tenant_admin=False)) as client:
        if str(client.base_url).rstrip("/") != DEMO:
            raise SystemExit("Refusing an unexpected API destination")
        publish(_common, client, apply=_common.APPLY, environment="DEMO",
                validation="Validated on LOCAL: step cases, call replays, the text suite and voice cases.")


if __name__ == "__main__":
    main()
