"""Backfill ``workflow_revisions`` with each workflow's CURRENT saved version.

Run once after migration a1b2c3d4e5f6, before publishing releases: a release
published later pins ``{workflow_id: current version}``, and the pin can only
be honoured after a later edit if a snapshot of that version exists. Without
this backfill the runtime keeps executing the latest save (and reports
``pinMissing``) until the workflow is saved once more.

Idempotent: a (workflow_id, version) that already has a snapshot is skipped.
Preview by default; ``--apply`` writes.

Run from the deploy root: env/bin/python scripts/backfill_workflow_revisions.py [--apply] [--tenant tn_x]
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.getcwd())

from sqlalchemy import select  # noqa: E402

from shared.db.mysql import get_sessionmaker  # noqa: E402
from shared.db.schema_features import table_exists  # noqa: E402
from shared.ids import new_id  # noqa: E402
from shared.models import Workflow, WorkflowRevision  # noqa: E402
from shared.orchestration.behavior import resolve_behavior  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--tenant", default=None)
    args = parser.parse_args()
    if not table_exists("workflow_revisions"):
        raise SystemExit("workflow_revisions table missing — apply migration a1b2c3d4e5f6 first")
    session = get_sessionmaker()()
    try:
        query = select(Workflow).where(Workflow.is_deleted.is_(False))
        if args.tenant:
            query = query.where(Workflow.tenant_id == args.tenant)
        existing = {
            (wf_id, version) for wf_id, version in
            session.execute(select(WorkflowRevision.workflow_id, WorkflowRevision.version)).all()
        }
        planned = 0
        for wf in session.execute(query).scalars():
            if not (wf.nodes or []) or (wf.id, wf.version) in existing:
                continue
            planned += 1
            print(f"{'WRITE ' if args.apply else 'would '}snapshot {wf.id} v{wf.version} "
                  f"({wf.tenant_id}/{wf.bot_id}) behavior v{resolve_behavior({'nodes': wf.nodes}).version}")
            if args.apply:
                session.add(WorkflowRevision(
                    id=new_id("wfr"), workflow_id=wf.id, tenant_id=wf.tenant_id, bot_id=wf.bot_id,
                    version=wf.version, name=wf.name, nodes=wf.nodes, edges=wf.edges,
                    behavior_version=resolve_behavior({"nodes": wf.nodes}).version,
                    created_by=None,
                ))
        if args.apply:
            session.commit()
        print(f"{'wrote' if args.apply else 'would write'} {planned} snapshot(s)")
    finally:
        session.close()


if __name__ == "__main__":
    main()
