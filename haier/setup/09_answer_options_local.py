"""Save the answer-options workflow revision on the LOCAL bot and release it.

Dry-run by default: prints what would be written and sends nothing. ``--apply``
writes through the local API only (a workflow revision and one release that
pins it); the script refuses any API that is not on this machine. It never
touches DEMO or production, copies no code and restarts no service.

    env/bin/python -B haier/setup/09_answer_options_local.py            # dry-run
    env/bin/python -B haier/setup/09_answer_options_local.py --apply

The running local API / voice worker keep the code they were started with:
restart them yourself to pick up the matching runtime (the workflow revision
alone changes nothing on a runtime that does not know ``answerOptions``).
Restart the API BEFORE running this with --apply: an API process that was
started before the runtime code changed stores the revision and then answers
HTTP 500 (it imports the changed engine lazily, after the commit). If that
happened, restart the API and run this again: it sees the stored revision
and creates only the release.
DEMO has its own script, ``10_answer_options_demo.py``.
"""
from __future__ import annotations

import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import _common  # noqa: E402
from haier.setup.haier_answer_options_publish import publish  # noqa: E402


def main() -> None:
    host = urlparse(_common.BASE).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"Refusing a non-local API: {_common.BASE}")
    _common.banner("Haier answer options: LOCAL workflow revision + release")
    with closing(_common.client(prefer_tenant_admin=False)) as client:
        publish(_common, client, apply=_common.APPLY, environment="LOCAL",
                validation="Step cases, call replays, the text suite and voice cases ran against this revision.")


if __name__ == "__main__":
    main()
