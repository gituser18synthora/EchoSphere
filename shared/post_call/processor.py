"""Durable post-call processing: enqueue at finalize, process in background.

Follows the repo's canonical durable-work recipe (knowledge ingestion): the
``conversation_memories`` row IS the job — status/attempts/error live on it,
claims are optimistic single-row UPDATEs so any number of pollers (voice
worker + telephony gateway both embed one) never double-process, and a
process restart loses nothing because queued rows persist and stale
``processing`` rows are reclaimed after a timeout.

Teardown is never delayed: :func:`enqueue_post_call` only INSERTs the queued
row (idempotent on the conversation id) and wakes the local poller; the LLM
analysis runs entirely in the background loop.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, update

from shared.config import get_settings
from shared.customer_context import phone_tail
from shared.db.mysql import get_sessionmaker
from shared.ids import new_id
from shared.models import ConversationMemory, ConversationSession
from shared.orchestration.goal_engine import compile_goal_policy
from shared.post_call.analyzer import (
    analyze_call,
    build_analysis_llm,
    fallback_analysis,
)
from shared.post_call.nba import decide_next_best_action
from shared.post_call.structured import merge_structured_fields

logger = logging.getLogger(__name__)

# Sentiment labels → the conversation row's coarse sentiment column.
_POSITIVE_HINTS = ("positive", "cooperative", "satisfied", "happy", "agree")
_NEGATIVE_HINTS = (
    "negative", "refus", "frustrat", "angry", "upset", "hostile", "firm",
)

# Local wake-up signal so a just-enqueued row is processed without waiting a
# full poll interval. One event per running worker loop.
_wake_events: set[asyncio.Event] = set()


def notify_post_call_worker() -> None:
    """Wake every worker loop in this process (cheap, thread-safe enough:
    Event.set() is atomic and idempotent)."""
    for event in list(_wake_events):
        event.set()


# ── enqueue (called from SessionRecorder.finalize) ─────────────────────────


def _workflow_position(events: list[dict]) -> tuple[bool, str | None]:
    """Whether the call ended inside a workflow, from the recorded events."""
    stage = None
    for event in reversed(events or []):
        if event.get("kind") == "orchestration_turn":
            stage = str(event.get("new_stage") or "")
            break
    if stage and stage.startswith("workflow:"):
        return True, stage
    return False, stage or None


_SLOT_SECRET_HINTS = ("otp", "pin", "cvv", "password", "card_number", "aadhaar")
_MAX_SLOT_SNAPSHOT_CHARS = 160
_MAX_SLOT_SNAPSHOT_KEYS = 48


def _snapshot_workflow_slots(slots: dict) -> dict[str, str]:
    """Bounded, string-only copy of the workflow slots for final_state."""
    out: dict[str, str] = {}
    for key, value in list(slots.items())[:_MAX_SLOT_SNAPSHOT_KEYS]:
        name = str(key or "").strip()
        if not name or value is None or isinstance(value, (dict, list)):
            continue
        if any(hint in name.lower() for hint in _SLOT_SECRET_HINTS):
            continue
        text = " ".join(str(value).split())[:_MAX_SLOT_SNAPSHOT_CHARS]
        if text:
            out[name] = text
    return out


def enqueue_post_call(recorder) -> str | None:
    """Create the queued memory row for one finalized call (sync, cheap).

    Idempotent on ``conversation_id`` — a duplicate hangup/finalize finds the
    existing row and does nothing. Returns the memory row id, or None when
    the call produced nothing to analyze (no turns at all) or the tenant has
    call-summary generation switched off.
    """
    if not recorder.turns:
        return None  # never-connected / silent call: nothing to remember
    # Tenant switch, resolved from the DB at enqueue time: generation off
    # means NO analysis job at all — transcript/call persistence, billing and
    # teardown all happened before this point and are untouched.
    from shared.post_call.tenant_flags import load_tenant_summary_flags_sync

    if not load_tenant_summary_flags_sync(recorder.config.tenant_id).call_summary_enabled:
        logger.info(
            "post_call_skipped %s",
            json.dumps({
                "conversation_id": recorder.control_plane_id,
                "tenant_id": recorder.config.tenant_id,
                "reason": "call_summary_disabled",
            }),
        )
        return None
    escalated = any(e.get("kind") == "handoff" for e in recorder.events)
    workflow_active, workflow_stage = _workflow_position(recorder.events)
    final_state = {
        # Final guided-flow answers (scalar slots, bounded, secret-named keys
        # dropped) — the authoritative source for structured summary fields.
        "workflow_slots": _snapshot_workflow_slots(
            getattr(recorder, "workflow_slots", None) or {}
        ),
        "disposition": recorder.disposition,
        "end_reason": recorder.end_reason,
        "call_state": recorder.call_state or {},
        "language": recorder.language,
        "escalated": escalated,
        "workflow_active": workflow_active,
        "workflow_stage": workflow_stage,
        "channel": recorder.channel,
        "duration_sec": int(time.time() - recorder.started_at),
        "started_at": datetime.fromtimestamp(
            recorder.started_at, tz=timezone.utc
        ).isoformat(),
        "domain_policy": "collections" if recorder.customer_context_id else "generic",
    }
    session = get_sessionmaker()()
    try:
        existing = session.execute(
            select(ConversationMemory.id).where(
                ConversationMemory.conversation_id == recorder.control_plane_id
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = ConversationMemory(
            id=new_id("cm"),
            tenant_id=recorder.config.tenant_id,
            conversation_id=recorder.control_plane_id,
            session_id=recorder.session_id,
            bot_id=recorder.config.bot_id,
            channel=recorder.channel,
            runtime_context_record_id=recorder.runtime_context_record_id,
            customer_context_id=recorder.customer_context_id,
            phone_tail=phone_tail(recorder.caller or "") or None,
            status="queued",
            max_attempts=get_settings().post_call_max_attempts,
            final_state=final_state,
            language=recorder.language,
        )
        session.add(row)
        session.commit()
        logger.info(
            "post_call_enqueued %s",
            json.dumps({
                "conversation_id": recorder.control_plane_id,
                "session_id": recorder.session_id,
                "disposition": recorder.disposition,
            }),
        )
        return row.id
    except Exception:  # noqa: BLE001 — a race on the unique key is a no-op
        session.rollback()
        existing = session.execute(
            select(ConversationMemory.id).where(
                ConversationMemory.conversation_id == recorder.control_plane_id
            )
        ).scalar_one_or_none()
        if existing is None:
            logger.exception(
                "post-call enqueue failed for %s", recorder.control_plane_id
            )
        return existing
    finally:
        session.close()


# ── claim / process ─────────────────────────────────────────────────────────


def _claim_next_sync() -> str | None:
    """Claim one row: queued first, then orphaned 'processing' rows."""
    settings = get_settings()
    stale_before = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        seconds=settings.post_call_stale_processing_seconds
    )
    session = get_sessionmaker()()
    try:
        for status, extra in (("queued", None), ("processing", stale_before)):
            query = select(ConversationMemory.id).where(
                ConversationMemory.status == status,
                ConversationMemory.is_deleted.is_(False),
            )
            if extra is not None:
                query = query.where(
                    ConversationMemory.started_processing_at.is_not(None),
                    ConversationMemory.started_processing_at < extra,
                )
            candidate = session.execute(
                query.order_by(ConversationMemory.created_at).limit(1)
            ).scalar_one_or_none()
            if candidate is None:
                continue
            claim = session.execute(
                update(ConversationMemory)
                .where(
                    ConversationMemory.id == candidate,
                    ConversationMemory.status == status,
                )
                .values(
                    status="processing",
                    attempts=ConversationMemory.attempts + 1,
                    started_processing_at=datetime.now(timezone.utc).replace(
                        tzinfo=None
                    ),
                )
            )
            session.commit()
            if claim.rowcount == 1:
                return candidate
        return None
    except Exception:  # noqa: BLE001 — a broken claim must not kill the loop
        session.rollback()
        logger.exception("post-call claim failed")
        return None
    finally:
        session.close()


def _sentiment_from(label: str) -> str | None:
    lowered = (label or "").lower()
    if any(hint in lowered for hint in _POSITIVE_HINTS):
        return "positive"
    if any(hint in lowered for hint in _NEGATIVE_HINTS):
        return "negative"
    return "neutral" if lowered else None


async def _load_transcript_doc(session_id: str) -> dict | None:
    from shared.db.mongo import Mongo

    try:
        return await Mongo.transcripts().find_one({"session_id": session_id})
    except Exception:  # noqa: BLE001
        logger.exception("post-call transcript load failed for %s", session_id)
        return None


def _persist_result_sync(
    row_id: str,
    analysis,
    *,
    status: str,
    error: str | None,
    usage: tuple[int, int] | None,
    engine_conf: dict | None,
) -> None:
    """Write the analysis onto the row + conversation, one transaction.

    The post-call LLM tokens become a usage event on the SAME session and the
    conversation's stored cost is updated in the same transaction, so the
    detail API's cost reconciliation invariant keeps holding.
    """
    session = get_sessionmaker()()
    try:
        row = session.get(ConversationMemory, row_id)
        if row is None:
            return
        nba = analysis.next_best_action
        row.status = status
        row.error = error
        row.generated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        row.call_outcome = analysis.call_outcome or None
        row.summary = analysis.summary or None
        row.memory = analysis.memory_payload()
        row.next_action = nba.action or None
        row.next_best_action = nba.model_dump(mode="json")
        row.follow_up_required = bool(analysis.follow_up_required)
        row.follow_up_at = (
            nba.recommended_at.astimezone(timezone.utc).replace(tzinfo=None)
            if nba.recommended_at
            else None
        )
        row.confidence = analysis.confidence
        row.dominant_language = analysis.dominant_language or None

        conversation = session.get(ConversationSession, row.conversation_id)
        if conversation is not None:
            sentiment = _sentiment_from(analysis.customer_sentiment)
            if sentiment:
                conversation.sentiment = sentiment
            if usage and any(usage) and engine_conf:
                from shared.billing.metering import record_usage_event

                event = record_usage_event(
                    session,
                    tenant_id=row.tenant_id,
                    bot_id=row.bot_id,
                    session_id=row.session_id,
                    capability="llm",
                    provider_code=engine_conf.get("provider") or "openai",
                    model_code=engine_conf.get("model") or "",
                    request_id=f"{row.session_id}:llm:postcall",
                    requests=1,
                    input_tokens=usage[0],
                    output_tokens=usage[1],
                    usage_source="provider",
                    usage_metadata={"purpose": "post_call_analysis"},
                    commit=False,
                )
                if event is not None:
                    # Keep the stored conversation total equal to the sum of
                    # its usage events (the detail API asserts this).
                    conversation.cost_usd = (
                        Decimal(str(conversation.cost_usd or 0))
                        + Decimal(str(event.cost_usd))
                    )
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("post-call result persistence failed for %s", row_id)
    finally:
        session.close()


def _requeue_or_fail_sync(row_id: str, error: str) -> str:
    """Failure bookkeeping: back to queued while attempts remain, else the
    caller persists the deterministic fallback under status 'failed'."""
    session = get_sessionmaker()()
    try:
        row = session.get(ConversationMemory, row_id)
        if row is None:
            return "gone"
        if row.attempts < row.max_attempts:
            row.status = "queued"
            row.error = error[:500]
            session.commit()
            return "requeued"
        row.error = error[:500]
        session.commit()
        return "exhausted"
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("post-call failure bookkeeping failed for %s", row_id)
        return "gone"
    finally:
        session.close()


def _analysis_engine_conf(config) -> dict:
    llm_conf = dict(config.llm or {})
    orchestration = llm_conf.get("orchestration") or {}
    return orchestration if orchestration.get("provider") else llm_conf


def _load_row_sync(row_id: str) -> dict | None:
    session = get_sessionmaker()()
    try:
        row = session.get(ConversationMemory, row_id)
        if row is None:
            return None
        return {
            "conversation_id": row.conversation_id,
            "session_id": row.session_id,
            "tenant_id": row.tenant_id,
            "bot_id": row.bot_id,
            "final_state": dict(row.final_state or {}),
            "language": row.language,
        }
    finally:
        session.close()


async def process_memory_row(row_id: str) -> bool:
    """Run the analysis for one claimed row. Returns True when completed."""
    started = time.perf_counter()
    row_data = await asyncio.to_thread(_load_row_sync, row_id)
    if row_data is None:
        return False

    logger.info(
        "post_call_started %s",
        json.dumps({"source_conversation_id": row_data["conversation_id"]}),
    )
    final_state = row_data["final_state"]
    policy = None

    try:
        from shared.bot_config import resolve_bot_config

        config = await resolve_bot_config(row_data["bot_id"], require_published=False)
        policy = compile_goal_policy(
            dict(getattr(config, "goal_policy", None) or {}),
            bot_name=config.bot_name,
            system_prompt=config.system_prompt,
            intents=config.intents,
            domain_policy=str(final_state.get("domain_policy") or "generic"),
        )
        doc = await _load_transcript_doc(row_data["session_id"])
        turns = list((doc or {}).get("turns") or [])
        events = list((doc or {}).get("events") or [])
        reference = datetime.now(timezone.utc)
        raw_started = final_state.get("started_at")
        if raw_started:
            try:
                reference = datetime.fromisoformat(str(raw_started))
            except ValueError:
                pass

        llm = build_analysis_llm(config)
        analysis, usage = await analyze_call(
            llm,
            policy=policy,
            turns=turns,
            events=events,
            final_state={
                key: final_state.get(key)
                for key in ("disposition", "end_reason", "language",
                            "escalated", "workflow_stage", "duration_sec",
                            "call_state")
            },
            reference=reference,
            timeout_seconds=get_settings().post_call_llm_timeout_seconds,
        )
        if analysis is None:
            raise RuntimeError("analysis_unavailable")

        # Deterministic reconciliation: recorded platform state outranks the
        # model's proposal, and unknown actions never persist.
        analysis.next_best_action = decide_next_best_action(
            analysis,
            policy=policy,
            disposition=final_state.get("disposition"),
            call_state=final_state.get("call_state") or {},
            escalated=bool(final_state.get("escalated")),
            workflow_active=bool(final_state.get("workflow_active")),
        )
        if analysis.next_best_action.action in (
            "do_not_contact", "close_goal_completed", "no_action",
        ):
            analysis.follow_up_required = False
        if not analysis.dominant_language:
            analysis.dominant_language = row_data["language"] or ""
        # Structured summary fields: the workflow's final (corrected) slots
        # outrank the analyst; the analyst only fills what the flow never
        # collected, clamped onto each field's vocabulary.
        analysis.structured_fields, analysis.structured_field_sources = (
            merge_structured_fields(
                policy, final_state.get("workflow_slots") or {},
                analysis.structured_fields,
            )
        )

        await asyncio.to_thread(
            _persist_result_sync,
            row_id,
            analysis,
            status="completed",
            error=None,
            usage=usage,
            engine_conf=_analysis_engine_conf(config),
        )
        logger.info(
            "post_call_completed %s",
            json.dumps({
                "source_conversation_id": row_data["conversation_id"],
                "outcome": analysis.call_outcome,
                "next_best_action_type": analysis.next_best_action.action,
                "summary_latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — retried, then terminal fallback
        verdict = await asyncio.to_thread(
            _requeue_or_fail_sync, row_id, f"{type(exc).__name__}: {exc}"
        )
        if verdict == "exhausted":
            # Terminal: persist the deterministic fallback so the customer's
            # next call still gets safe context; the transcript is untouched.
            fallback = fallback_analysis(final_state=final_state, policy=policy)
            await asyncio.to_thread(
                _persist_result_sync,
                row_id,
                fallback,
                status="failed",
                error=f"{type(exc).__name__}: {exc}"[:500],
                usage=None,
                engine_conf=None,
            )
        logger.warning(
            "post_call_failed %s",
            json.dumps({
                "source_conversation_id": row_data["conversation_id"],
                "error": f"{type(exc).__name__}: {exc}"[:200],
                "verdict": verdict,
                "summary_latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }),
        )
        return False


async def run_pending_once(limit: int = 10) -> int:
    """Claim-and-process up to ``limit`` rows; returns how many completed.

    A row that failed and was requeued is NOT re-claimed within the same
    pass — retries are spaced by the poll interval instead of burning every
    attempt back-to-back against a provider that is down right now.
    """
    done = 0
    seen: set[str] = set()
    for _ in range(limit):
        row_id = await asyncio.to_thread(_claim_next_sync)
        if row_id is None or row_id in seen:
            if row_id is not None:
                # Hand the claim back untouched; the next pass retries it.
                await asyncio.to_thread(_unclaim_sync, row_id)
            break
        seen.add(row_id)
        if await process_memory_row(row_id):
            done += 1
    return done


def _unclaim_sync(row_id: str) -> None:
    """Return a claimed-but-unprocessed row to the queue (attempt not used)."""
    session = get_sessionmaker()()
    try:
        session.execute(
            update(ConversationMemory)
            .where(
                ConversationMemory.id == row_id,
                ConversationMemory.status == "processing",
            )
            .values(status="queued", attempts=ConversationMemory.attempts - 1)
        )
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
    finally:
        session.close()


async def run_worker(stop_event: asyncio.Event | None = None) -> None:
    """Poll loop, embedded in the voice worker/gateway lifespan.

    Multiple processes may run this concurrently: claims are single-row
    optimistic updates, so a row is only ever processed by one of them.
    """
    stop_event = stop_event or asyncio.Event()
    wake = asyncio.Event()
    _wake_events.add(wake)
    poll = get_settings().post_call_poll_seconds
    logger.info("post-call worker started (poll=%.1fs)", poll)
    try:
        while not stop_event.is_set():
            try:
                await run_pending_once()
            except Exception:  # noqa: BLE001 — the loop must survive anything
                logger.exception("post-call worker iteration failed")
                await asyncio.sleep(5)
            wake.clear()
            try:
                await asyncio.wait_for(
                    asyncio.create_task(_first(stop_event.wait(), wake.wait())),
                    timeout=poll,
                )
            except asyncio.TimeoutError:
                pass
    finally:
        _wake_events.discard(wake)
        logger.info("post-call worker stopped")


async def _first(*aws) -> None:
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
