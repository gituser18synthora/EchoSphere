"""Opaque prefixed IDs, consistent with the existing engine style (vb_<hex12>)."""

import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
