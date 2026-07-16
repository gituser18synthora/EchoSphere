"""Standard JSON response envelope.

Success: {"success": true, "data": ..., "meta": {...}?}
Error:   {"success": false, "message": "...", "errors": [...]?}
"""

from typing import Any


def ok(data: Any = None, meta: dict | None = None) -> dict:
    body: dict = {"success": True, "data": data}
    if meta is not None:
        body["meta"] = meta
    return body


def paginated(items: list, *, page: int, page_size: int, total: int) -> dict:
    return ok(
        items,
        meta={
            "page": page,
            "pageSize": page_size,
            "total": total,
            "totalPages": max(1, -(-total // page_size)) if page_size else 1,
        },
    )
