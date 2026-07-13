"""Shared MongoDB patch helpers and Redis cache invalidation."""

from typing import Any

from pymongo import ReturnDocument

from voicebot.config_layer.cache import ConfigCache
from voicebot.config_layer.db import MongoDB

cache = ConfigCache()


def flatten_for_set(prefix: str, data: dict[str, Any]) -> dict[str, Any]:
    """
    Turn a nested dict into MongoDB $set keys with dot notation.
    Lists and scalars are set at the leaf path as-is.
    """
    out: dict[str, Any] = {}

    def walk(base: str, obj: Any) -> None:
        if isinstance(obj, dict):
            if not obj:
                out[base] = {}
                return
            for k, v in obj.items():
                path = f"{base}.{k}" if base else k
                walk(path, v)
        else:
            out[base] = obj

    walk(prefix, data)
    return out


async def apply_voicebot_patch(
    voicebot_id: str,
    set_fields: dict[str, Any],
    *,
    upsert: bool = False,
) -> dict | None:
    """
    $set multiple dotted paths in one update. Invalidates Redis cache.
    Returns the full document after update, or None if no match.
    """
    result = await MongoDB.voicebot_configs().find_one_and_update(
        {"voicebot_id": voicebot_id},
        {"$set": set_fields},
        upsert=upsert,
        return_document=ReturnDocument.AFTER,
    )
    if result is not None:
        await cache.invalidate(voicebot_id)
    return result


async def patch_voicebot_section(
    voicebot_id: str,
    section_key: str,
    section_data: dict[str, Any],
) -> dict:
    """
    Merge-patch a single top-level section using dotted $set keys.
    Returns the full updated document.
    """
    flat = flatten_for_set(section_key, section_data)
    result = await apply_voicebot_patch(voicebot_id, flat)
    if not result:
        raise ValueError(f"VoiceBot {voicebot_id} not found")
    return result


async def get_tab_section(voicebot_id: str, section_key: str) -> dict | None:
    """Fetch only the relevant section for a tab GET."""
    doc = await MongoDB.voicebot_configs().find_one(
        {"voicebot_id": voicebot_id},
        {section_key: 1, "_id": 0},
    )
    if not doc:
        return None
    return doc.get(section_key)
