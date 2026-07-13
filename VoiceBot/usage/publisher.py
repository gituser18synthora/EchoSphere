"""Publish voicebot per-call usage (STT / LLM / TTS) to Kafka Ai_Usage topic."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv

from messaging.schemas import UsageRecord

from voicebot.usage.tracker import CallUsageStats

if TYPE_CHECKING:
    from voicebot.config_layer.models import EngineConfig
    from voicebot.orchestrator.call_state import CallState

logger = logging.getLogger(__name__)

# Load .env before reading KAFKA_* (voicebot Settings does not export all keys to os.environ).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_VOICEBOT_DIR = _PROJECT_ROOT / "voicebot"
for _env_path in (_PROJECT_ROOT / ".env", _VOICEBOT_DIR / ".env"):
    if _env_path.is_file():
        load_dotenv(_env_path, override=False)


def _usage_topic() -> str:
    return os.getenv("KAFKA_USAGE_TOPIC", "Ai_Usage")


def _kafka_enabled() -> bool:
    return os.getenv("KAFKA_ENABLED", "false").lower() in ("true", "1", "yes")


def _float_env(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def build_usage_records(
    *,
    call_state: "CallState",
    engine: "EngineConfig",
    usage: CallUsageStats,
    llm_input_cost_per_1k: float,
    llm_output_cost_per_1k: float,
    stt_cost_per_sec: float,
    tts_cost_per_10k_chars: float,
    tts_cost_currency: str,
) -> list[dict]:
    """Build UsageRecord payloads for stt_cost, llm_cost, and tts_cost features."""
    docs: list[dict] = []
    now = datetime.now(timezone.utc)
    tenant_id = str(call_state.tenant_id)
    call_id = str(call_state.call_id)
    base_meta = {
        "voicebot_id": call_state.voicebot_id,
        "turn_count": call_state.turn_count,
        "call_duration_seconds": round(call_state.call_duration_seconds(), 2),
    }

    if usage.stt_audio_seconds > 0:
        stt_cost = round(usage.stt_audio_seconds * stt_cost_per_sec, 6)
        docs.append(
            UsageRecord(
                call_id=call_id,
                tenant_id=tenant_id,
                source="voicebot",
                feature="stt_cost",
                provider_name=engine.stt_provider_id,
                model_name=engine.stt_provider_id,
                quantity=round(usage.stt_audio_seconds, 4),
                units="seconds",
                tokens_input=0,
                tokens_output=0,
                provider_cost=stt_cost,
                status="success",
                created_at=now,
                metadata={**base_meta, "currency": "USD"},
            ).model_dump(mode="json")
        )

    if usage.llm_total_tokens > 0:
        llm_cost = round(
            usage.llm_input_tokens / 1000 * llm_input_cost_per_1k
            + usage.llm_output_tokens / 1000 * llm_output_cost_per_1k,
            6,
        )
        docs.append(
            UsageRecord(
                call_id=call_id,
                tenant_id=tenant_id,
                source="voicebot",
                feature="llm_cost",
                provider_name=engine.llm_provider_id,
                model_name=engine.llm_model_id,
                quantity=usage.llm_total_tokens,
                units="tokens",
                tokens_input=usage.llm_input_tokens,
                tokens_output=usage.llm_output_tokens,
                provider_cost=llm_cost,
                status="success",
                created_at=now,
                metadata={**base_meta, "currency": "USD"},
            ).model_dump(mode="json")
        )

    if usage.tts_character_count > 0 or usage.tts_audio_seconds > 0:
        if usage.tts_character_count > 0:
            quantity = usage.tts_character_count
            units = "characters"
            tts_cost = round(quantity / 10_000 * tts_cost_per_10k_chars, 6)
        else:
            quantity = round(usage.tts_audio_seconds, 4)
            units = "seconds"
            tts_cost = 0.0
        tts_meta = {
            **base_meta,
            "currency": tts_cost_currency,
            "tts_audio_seconds": round(usage.tts_audio_seconds, 4),
        }
        if usage.tts_character_count > 0:
            tts_meta["tts_character_count"] = usage.tts_character_count
        docs.append(
            UsageRecord(
                call_id=call_id,
                tenant_id=tenant_id,
                source="voicebot",
                feature="tts_cost",
                provider_name=engine.tts_provider_id,
                model_name=engine.tts_provider_id,
                quantity=quantity,
                units=units,
                tokens_input=0,
                tokens_output=0,
                provider_cost=tts_cost,
                status="success",
                created_at=now,
                metadata=tts_meta,
            ).model_dump(mode="json")
        )

    return docs


async def publish_call_usage(
    call_state: "CallState",
    engine: "EngineConfig",
    usage: CallUsageStats,
) -> None:
    """Publish feature-wise voicebot usage to Kafka (non-fatal on failure)."""
    if not _kafka_enabled():
        logger.info(
            "[usage] Kafka publish skipped (KAFKA_ENABLED=%r) | call_id=%s",
            os.getenv("KAFKA_ENABLED"),
            call_state.call_id,
        )
        return

    topic = _usage_topic()
    bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")

    docs = build_usage_records(
        call_state=call_state,
        engine=engine,
        usage=usage,
        llm_input_cost_per_1k=_float_env("VOICEBOT_LLM_INPUT_COST_1K", 0.00015),
        llm_output_cost_per_1k=_float_env("VOICEBOT_LLM_OUTPUT_COST_1K", 0.00060),
        stt_cost_per_sec=_float_env("VOICEBOT_STT_COST_PER_SEC", 0.0001),
        tts_cost_per_10k_chars=_float_env("VOICEBOT_TTS_COST_PER_10K_CHARS", 30.0),
        tts_cost_currency=os.getenv("VOICEBOT_TTS_COST_CURRENCY", "INR").strip() or "INR",
    )
    if not docs:
        logger.debug(
            "[usage] No voicebot usage to publish | call_id=%s", call_state.call_id,
        )
        return

    try:
        from messaging.kafka_manager import get_kafka_producer

        logger.info(
            "[usage] Publishing %d record(s) to topic=%s broker=%s | call_id=%s",
            len(docs),
            topic,
            bootstrap,
            call_state.call_id,
        )
        producer = await get_kafka_producer()
        tasks = [
            producer.publish_usage(
                topic=topic,
                usage_doc=doc,
                key=doc.get("tenant_id"),
            )
            for doc in docs
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        ok = sum(1 for r in results if r is True)
        for i, r in enumerate(results):
            if r is not True:
                logger.warning(
                    "[usage] Publish failed for feature=%s: %s",
                    docs[i].get("feature"),
                    r,
                )
        logger.info(
            "[usage] Kafka published %d/%d for call_id=%s (voicebot)",
            ok,
            len(docs),
            call_state.call_id,
        )
        if ok < len(docs):
            logger.warning(
                "[usage] Kafka partial failure for call_id=%s", call_state.call_id,
            )
    except Exception as exc:
        logger.warning(
            "[usage] Kafka publish error (non-fatal) call_id=%s: %s",
            call_state.call_id,
            exc,
        )
