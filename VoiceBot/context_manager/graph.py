# context_manager/graph.py

import logging
from datetime import datetime, timedelta
from typing import Any

from voicebot.config_layer.db import COLLECTION_CALLER_GRAPHS, MongoDB
from voicebot.orchestrator.call_state import CallState

logger = logging.getLogger(__name__)


def _as_datetime(val: Any) -> datetime | None:
    """Normalize BSON/datetime/str to naive UTC datetime for comparisons."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=None) if val.tzinfo else val
    if isinstance(val, str):
        v = val.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(v)
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except ValueError:
            return None
    return None


class CallerGraph:
    """
    Manages cross-call caller knowledge graph in MongoDB.

    One document per (voicebot_id, caller_phone) pair.
    """

    async def load(
        self,
        voicebot_id: str,
        caller_phone: str,
        memory_expiry_days: int,
    ) -> dict | None:
        """
        Load caller graph at call start.
        Returns full graph dict or None for first-time caller.
        """
        db = MongoDB.db()
        doc = await db[COLLECTION_CALLER_GRAPHS].find_one({
            "voicebot_id": voicebot_id,
            "caller_phone": caller_phone,
        })

        if doc is None:
            logger.info(
                "first_time_caller",
                extra={
                    "voicebot_id": voicebot_id,
                    "caller_phone": caller_phone,
                },
            )
            return None

        now = datetime.utcnow()
        max_age = timedelta(days=memory_expiry_days)

        def _last_seen_key(n: dict) -> datetime:
            d = _as_datetime(n.get("last_seen"))
            return d or datetime.min

        # Filter stale nodes
        active_nodes = []
        for n in doc.get("nodes", []):
            ls = _as_datetime(n.get("last_seen"))
            if ls is None:
                continue
            if ls > now - max_age:
                active_nodes.append(n)

        active_nodes.sort(key=_last_seen_key, reverse=True)
        active_node_ids = {n["node_id"] for n in active_nodes}

        # Filter edges to only active nodes
        active_edges = [
            e for e in doc.get("edges", [])
            if e.get("from_node") in active_node_ids
            and e.get("to_node") in active_node_ids
        ]

        # Identify unresolved issues
        unresolved_ids = {
            e["to_node"] for e in active_edges
            if e.get("relation") == "unresolved"
        }

        recent_calls = doc.get("call_history", [])[-3:]

        result = {
            "caller_name": doc.get("caller_name"),
            "caller_email": doc.get("caller_email"),
            "nodes": active_nodes[:20],
            "edges": active_edges,
            "unresolved_node_ids": list(unresolved_ids),
            "recent_calls": recent_calls,
        }

        logger.info(
            "caller_graph_loaded",
            extra={
                "caller_phone": caller_phone,
                "nodes": len(result["nodes"]),
                "edges": len(result["edges"]),
                "recent_calls": len(recent_calls),
            },
        )
        return result

    async def write(
        self,
        call_state: CallState,
        extraction: dict,
        memory_expiry_days: int,
    ) -> None:
        """
        Write extracted graph data to MongoDB at call end.
        Uses upsert semantics throughout.

        Fixes:
        - call_duration_seconds and all config-driven standard/custom fields
          are written into the call_entry from extraction
        - caller_name / caller_email only overwritten when extraction has a real value
        - person_caller.last_seen only updated when turn_count > 0
        - orphan guard: any node missing an edge to person_caller gets one auto-added
        """
        if extraction is None or not extraction:
            logger.warning("No extraction data to write")
            return

        db = MongoDB.db()
        now = datetime.utcnow()
        expires_at = now + timedelta(days=memory_expiry_days)

        filter_doc = {
            "voicebot_id": call_state.voicebot_id,
            "caller_phone": call_state.caller_phone,
        }

        # Collect all extracted standard/custom field values (everything except
        # the known structural keys — those are handled separately).
        _structural_keys = {"caller_name", "caller_email", "nodes", "edges", "summary"}
        extracted_fields: dict = {
            k: v for k, v in extraction.items() if k not in _structural_keys
        }

        call_entry = {
            "call_id": call_state.call_id,
            "summary": extraction.get("summary", ""),
            "sentiment": call_state.sentiment_trend,
            "turn_count": call_state.turn_count,
            "date": now,
            **extracted_fields,  # call_duration_seconds, language_detected, goal_outcome, etc.
        }

        # Only overwrite caller_name / caller_email when extraction has a real value.
        # Never replace an existing name with null.
        base_set: dict = {
            "updated_at": now,
            "expires_at": expires_at,
        }
        if extraction.get("caller_name"):
            base_set["caller_name"] = extraction["caller_name"]
        if extraction.get("caller_email"):
            base_set["caller_email"] = extraction["caller_email"]

        # Same field must not appear in both $set and $setOnInsert (MongoDB error 40).
        set_on_insert: dict = {
            "tenant_id": call_state.tenant_id,
            "created_at": now,
            "nodes": [],
            "edges": [],
        }
        if "caller_name" not in base_set:
            set_on_insert["caller_name"] = None
        if "caller_email" not in base_set:
            set_on_insert["caller_email"] = None

        await db[COLLECTION_CALLER_GRAPHS].update_one(
            filter_doc,
            {
                "$setOnInsert": set_on_insert,
                "$set": base_set,
                "$push": {
                    "call_history": {
                        "$each": [call_entry],
                        "$slice": -10,
                    }
                },
            },
            upsert=True,
        )

        # Orphan guard: collect node_ids that already have an edge pointing to them
        edges = list(extraction.get("edges", []))
        edge_targets = {e.get("to_node") for e in edges if e.get("to_node")}

        # Write non-person nodes
        nodes = extraction.get("nodes", [])
        for node in nodes:
            node_id = node.get("node_id")
            if not node_id or node_id == "person_caller":
                continue

            # last_seen only updated on calls with actual conversation turns
            node_last_seen = now if call_state.turn_count > 0 else None

            result = await db[COLLECTION_CALLER_GRAPHS].update_one(
                {
                    **filter_doc,
                    "nodes": {"$elemMatch": {"node_id": node_id}},
                },
                {
                    "$set": {
                        "nodes.$.value": node.get("value", ""),
                        "nodes.$.confidence": node.get("confidence", 0.8),
                        **({"nodes.$.last_seen": node_last_seen} if node_last_seen else {}),
                        "nodes.$.source_call_id": call_state.call_id,
                    }
                },
            )

            if result.modified_count == 0:
                await db[COLLECTION_CALLER_GRAPHS].update_one(
                    filter_doc,
                    {
                        "$push": {
                            "nodes": {
                                "node_id": node_id,
                                "type": node.get("type", "topic"),
                                "key": node.get("key", ""),
                                "value": node.get("value", ""),
                                "confidence": node.get("confidence", 0.8),
                                "first_seen": now,
                                "last_seen": now,
                                "source_call_id": call_state.call_id,
                            }
                        }
                    },
                )

            # Orphan guard: auto-add edge if this node has no edge pointing to it
            if node_id not in edge_targets:
                logger.warning(
                    "orphan_node_detected | node_id=%s — auto-adding edge from person_caller",
                    node_id,
                )
                edges.append({
                    "from_node": "person_caller",
                    "to_node": node_id,
                    "relation": "has_fact",
                })
                edge_targets.add(node_id)

        # Update person_caller last_seen only when turn_count > 0
        if call_state.turn_count > 0:
            result = await db[COLLECTION_CALLER_GRAPHS].update_one(
                {
                    **filter_doc,
                    "nodes": {"$elemMatch": {"node_id": "person_caller"}},
                },
                {
                    "$set": {
                        "nodes.$.last_seen": now,
                        "nodes.$.source_call_id": call_state.call_id,
                    }
                },
            )
            if result.modified_count == 0:
                await db[COLLECTION_CALLER_GRAPHS].update_one(
                    filter_doc,
                    {
                        "$push": {
                            "nodes": {
                                "node_id": "person_caller",
                                "type": "person",
                                "key": "caller",
                                "value": call_state.caller_phone,
                                "confidence": 1.0,
                                "first_seen": now,
                                "last_seen": now,
                                "source_call_id": call_state.call_id,
                            }
                        }
                    },
                )

        # Write edges (including any auto-added by orphan guard)
        for edge in edges:
            from_id = edge.get("from_node")
            to_id = edge.get("to_node")
            relation = edge.get("relation", "discussed")

            if not from_id or not to_id:
                continue

            result = await db[COLLECTION_CALLER_GRAPHS].update_one(
                {
                    **filter_doc,
                    "edges": {
                        "$elemMatch": {
                            "from_node": from_id,
                            "to_node": to_id,
                        }
                    },
                },
                {
                    "$set": {
                        "edges.$.relation": relation,
                        "edges.$.source_call_id": call_state.call_id,
                        "edges.$.created_at": now,
                    }
                },
            )

            if result.modified_count == 0:
                await db[COLLECTION_CALLER_GRAPHS].update_one(
                    filter_doc,
                    {
                        "$push": {
                            "edges": {
                                "from_node": from_id,
                                "to_node": to_id,
                                "relation": relation,
                                "source_call_id": call_state.call_id,
                                "created_at": now,
                            }
                        }
                    },
                )

        logger.info(
            "caller_graph_written",
            extra={
                "call_id": call_state.call_id,
                "caller_phone": call_state.caller_phone,
                "turn_count": call_state.turn_count,
                "nodes_processed": len(nodes),
                "edges_processed": len(edges),
                "extracted_fields": list(extracted_fields.keys()),
            },
        )

    async def delete_caller(
        self,
        voicebot_id: str,
        caller_phone: str,
    ) -> bool:
        """Delete entire caller graph. Returns True if document was deleted."""
        db = MongoDB.db()
        result = await db[COLLECTION_CALLER_GRAPHS].delete_one({
            "voicebot_id": voicebot_id,
            "caller_phone": caller_phone,
        })
        deleted = result.deleted_count > 0
        logger.info(
            "caller_graph_deleted",
            extra={
                "caller_phone": caller_phone,
                "deleted": deleted,
            },
        )
        return deleted

    async def delete_node(
        self,
        voicebot_id: str,
        caller_phone: str,
        node_id: str,
    ) -> None:
        """
        Delete specific node AND all its edges (no orphaned edges).
        """
        db = MongoDB.db()
        filter_doc = {
            "voicebot_id": voicebot_id,
            "caller_phone": caller_phone,
        }

        await db[COLLECTION_CALLER_GRAPHS].update_one(
            filter_doc,
            {"$pull": {"nodes": {"node_id": node_id}}},
        )

        # Pull edges referencing this node (two ops for driver compatibility)
        await db[COLLECTION_CALLER_GRAPHS].update_one(
            filter_doc,
            {"$pull": {"edges": {"from_node": node_id}}},
        )
        await db[COLLECTION_CALLER_GRAPHS].update_one(
            filter_doc,
            {"$pull": {"edges": {"to_node": node_id}}},
        )
        logger.info(
            "node_deleted",
            extra={"node_id": node_id, "caller_phone": caller_phone},
        )

    async def delete_all_for_voicebot(
        self,
        voicebot_id: str,
    ) -> int:
        """Delete all caller graphs for a voicebot. Returns deleted count."""
        db = MongoDB.db()
        result = await db[COLLECTION_CALLER_GRAPHS].delete_many({
            "voicebot_id": voicebot_id,
        })
        logger.info(
            "all_graphs_deleted_for_voicebot",
            extra={
                "voicebot_id": voicebot_id,
                "count": result.deleted_count,
            },
        )
        return result.deleted_count