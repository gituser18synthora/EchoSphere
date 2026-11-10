"""Per-call transcript/event/usage recording.

- MongoDB: transcript (`conversation_transcripts`) and voice events
  (`voice_events`) — written asynchronously, never in the audio critical path.
- MySQL: a `conversation_sessions` row is created at call end (summary/usage).
"""

import asyncio
import logging
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from shared.bot_config import ResolvedBotConfig
from shared.db.mongo import Mongo
from shared.ids import new_id
from shared.knowledge.security import mask_pii

logger = logging.getLogger(__name__)


@dataclass
class TurnRecord:
    role: str  # user | bot
    text: str
    timestamp: float = field(default_factory=time.time)
    route: str | None = None
    kb_used: bool = False
    kb_sources: list[dict] = field(default_factory=list)
    latency_ms: dict = field(default_factory=dict)


class SessionRecorder:
    """Accumulates turns/events/usage for one call and persists them."""

    def __init__(self, session_id: str, config: ResolvedBotConfig, channel: str = "browser",
                 caller: str | None = None, call_id: str | None = None) -> None:
        self.session_id = session_id
        self.config = config
        self.channel = channel
        self.caller = caller
        # The telephony provider's call id (webhook callId / CallSid): the
        # handle an integrator holds, persisted on the conversation row.
        self.call_id = (str(call_id)[:64] or None) if call_id else None
        self.started_at = time.time()
        self.turns: list[TurnRecord] = []
        self.events: list[dict] = []
        self.usage: dict[str, float] = {
            "stt_seconds": 0.0, "stt_requests": 0,
            "llm_input_tokens": 0, "llm_output_tokens": 0,
            "llm_cached_tokens": 0, "llm_reasoning_tokens": 0,
            "llm_requests": 0, "llm_usage_estimated": 0,
            # Goal Engine decision calls — billed under the decision model.
            "decision_llm_requests": 0, "decision_llm_input_tokens": 0,
            "decision_llm_output_tokens": 0, "decision_llm_cached_tokens": 0,
            "tts_characters": 0, "kb_searches": 0,
            "stt_characters": 0,
        }
        # Billable STT audio, accumulated as Decimal from what the provider
        # actually reported (Sarvam finals carry metrics.audio_duration; the
        # segmented REST path measures the exact PCM). The usage dict above
        # mirrors it as float for the Mongo transcript document only.
        self._stt_seconds = Decimal(0)
        self._stt_basis: str | None = None  # provider_metrics | pcm
        self._stt_request_ids: set[str] = set()
        # Keep background event-persistence tasks alive until they complete.
        self._background_flushes: set[asyncio.Task] = set()
        # Per-engine TTS breakdown ("provider|model|voice" → counters) so a
        # mid-call fallback bills each provider for what it actually spoke.
        self.tts_usage: dict[str, dict] = {}
        # Conversation-model calls of THIS call: the request options they
        # sent (set at call start) and the service tier OpenAI reported per
        # completed call ({"fast": 21}) — see ConversationBrain._note_llm_call.
        self.llm_request_options: dict | None = None
        self.llm_service_tiers: dict[str, int] = {}
        self.end_reason: str | None = None
        # finalize() must run exactly once per call — a duplicate teardown
        # path (or a replayed hangup signal) must never re-bill, re-summarize
        # or re-enqueue post-call work.
        self._finalized = False
        # FreeSWITCH's account of the disconnect (raw Hangup-Cause plus the
        # normalized reason), set exactly once by the hangup monitor.
        self.hangup: dict | None = None
        # True once a telephony transfer control actually reached the wire.
        # Teardown branches on it: a transferred caller now belongs to the
        # human agent, so the channel is never killed and the end reason is
        # "transferred" rather than a generic shutdown.
        self.transferred: bool = False
        # Call outcome captured by the conversation policy (promise_to_pay,
        # payment_claimed, wrong_number, account_disputed, callback_requested,
        # complaint_recorded, escalated, …) — updated live by the brain.
        self.disposition: str | None = None
        # The customer_contexts row this call ran against, plus the
        # call-state fields to write back to it at finalize.
        self.customer_context_id: str | None = None
        # The generic runtime_context_records row (tenant-defined context),
        # when the call ran on one — call_state merges into its JSON blob.
        self.runtime_context_record_id: str | None = None
        self.call_state: dict = {}
        # Latest workflow slot values (the brain replaces this after every
        # workflow turn) — the authoritative, correction-aware answers the
        # post-call structured summary is derived from.
        self.workflow_slots: dict = {}
        # Conversation language, live: starts at the bot default and follows
        # the caller (the brain updates it on every detected switch).
        self.language: str = config.language
        # Control-plane row id, fixed up front so the Mongo transcript and the
        # MySQL conversation_sessions row are created already linked (the API
        # looks the transcript up by this id).
        self.control_plane_id: str = new_id("cv")
        # The call's guardrail engine (set at call start). Owns transcript
        # redaction and the trigger ledger persisted at finalize.
        self.guardrails = None
        self._guardrail_triggers_persisted = False
        # Set by CallRecordingWriter when call audio was captured.
        self.recording_info: dict | None = None
        # The active audio writer (if recording is enabled) — finalized here
        # because pipeline teardown does not reliably deliver Cancel/End frames
        # to processors sitting after the output transport.
        self.recording_writer: "CallRecordingWriter | None" = None
        self.transcript_playout_only = False
        from voice_runtime.transcript_playout import PlayoutLedger
        self.playout_ledger = PlayoutLedger()

    def set_recording(self, *, path: str, duration_sec: float, sample_rate: int,
                      channels: int, size_bytes: int) -> None:
        self.recording_info = {
            "path": path,
            "mimeType": "audio/wav",
            "durationSec": round(duration_sec, 1),
            "sampleRate": sample_rate,
            "channels": channels,
            "sizeBytes": size_bytes,
        }

    def add_turn(self, turn: TurnRecord) -> None:
        self.turns.append(turn)

    def transcript_rows(self) -> list[dict]:
        """Default rows are unchanged; opt-in uses confirmed delivery only."""
        rows = []
        self.playout_ledger.used.clear()
        for turn in self.turns:
            spoken, delivery = turn.text, None
            if self.transcript_playout_only and turn.role == "bot":
                spoken, delivery = self.playout_ledger.for_turn(turn.text)
            row = {
                "role": turn.role, "text": self._redact_for_transcript(spoken),
                "ts": turn.timestamp, "route": turn.route, "kbUsed": turn.kb_used,
                "kbSources": turn.kb_sources, "latencyMs": turn.latency_ms,
            }
            if delivery is not None:
                row["delivery"] = delivery
            rows.append(row)
        return rows

    def stt_transcript_characters(self) -> int:
        """Characters of accepted caller speech, from the final turn list.

        Computed at billing time instead of accumulated per turn: barge-in
        rewinds and fragment merges pop a user turn and re-run its text as
        part of a merged turn, and a running counter double-counts exactly
        those paths. The turn list holds the pre-redaction text, so this is
        the actual STT output.
        """
        return sum(len(t.text or "") for t in self.turns if t.role == "user")

    def _redact_for_transcript(self, text: str) -> str:
        """Profile-driven redaction for stored turn text. Falls back to the
        pre-profile PII masking when no engine was attached — stored
        transcripts are never less protected than before."""
        if self.guardrails is not None:
            return self.guardrails.redact_for_persistence(text)
        return mask_pii(text, kinds={"card_number", "aadhaar", "pan"})

    def add_stt_usage(
        self,
        *,
        seconds: "Decimal | float | str",
        request_id: str | None = None,
        basis: str = "provider_metrics",
    ) -> bool:
        """Fold one final STT result's billable audio duration into the call.

        ``seconds`` is the provider-reported duration (Sarvam final
        ``metrics.audio_duration``) or the exact PCM length on the segmented
        REST path. Deduplicated by provider ``request_id`` so an SDK callback
        replay or reconnect re-delivery of the same final never double-bills.
        Returns True when the duration was counted.
        """
        if request_id:
            if request_id in self._stt_request_ids:
                return False
            self._stt_request_ids.add(request_id)
        try:
            duration = Decimal(str(seconds))
        except (InvalidOperation, TypeError, ValueError):
            return False
        if duration <= 0:
            return False
        self._stt_seconds += duration
        self._stt_basis = self._stt_basis or basis
        self.usage["stt_seconds"] = float(self._stt_seconds)
        self.usage["stt_requests"] = int(self.usage.get("stt_requests", 0)) + 1
        return True

    def configured_models(self) -> dict:
        """provider/model of each engine in this call's config snapshot."""
        def label(conf: dict | None) -> str | None:
            conf = conf or {}
            if not conf.get("model"):
                return None
            return f"{conf.get('provider') or '?'}/{conf['model']}"

        llm = self.config.llm or {}
        models = {
            "stt": label(self.config.stt),
            "tts": label(self.config.tts),
            "llm": label(llm),
            "decision": label(llm.get("orchestration")) or label(llm),
        }
        # What the conversation-model calls (replies, rewrites, extractors,
        # classifier) actually sent, and the tier OpenAI reported for them.
        if self.llm_request_options:
            models["llm_options"] = dict(self.llm_request_options)
        if self.llm_service_tiers:
            models["llm_service_tiers"] = dict(self.llm_service_tiers)
        return models

    def add_tts_usage(self, *, provider: str, model: str, voice: str, characters: int) -> None:
        """Called by the TTS router once per completed generation."""
        self.usage["tts_characters"] += characters
        key = f"{provider}|{model}|{voice}"
        entry = self.tts_usage.setdefault(
            key, {"provider": provider, "model": model, "voice": voice,
                  "characters": 0, "requests": 0}
        )
        entry["characters"] += characters
        entry["requests"] += 1

    def set_hangup(self, info: dict) -> bool:
        """Record how FreeSWITCH says the call ended. First writer wins —
        duplicate event deliveries can never rewrite the disconnect verdict.
        Returns True when this call stored it."""
        if self.hangup is not None:
            return False
        self.hangup = dict(info)
        return True

    def add_event(self, kind: str, **data) -> None:
        self.events.append({
            "kind": kind,
            "at": datetime.now(timezone.utc).isoformat(),
            **data,
        })

    async def flush_event(self, kind: str, **data) -> None:
        """Persist a single event immediately (barge-in, handoff, errors)."""
        self.add_event(kind, **data)
        await self._persist_event(kind, data)

    def flush_event_soon(self, kind: str, **data) -> None:
        """Record the event now, persist it in the background.

        For events emitted on the realtime path (interruption handling, the
        window between a transcript final and the reply's first audio): the
        in-memory record still reaches the transcript at finalize, but the
        Mongo round trip must never sit ahead of caller-audible work — a
        degraded Mongo (serverSelectionTimeoutMS) otherwise stalls the call
        for seconds per event.
        """
        self.add_event(kind, **data)
        try:
            task = asyncio.get_running_loop().create_task(
                self._persist_event(kind, data)
            )
        except RuntimeError:
            return  # no running loop (sync tests) — the in-memory event stands
        self._background_flushes.add(task)
        task.add_done_callback(self._background_flushes.discard)

    async def _persist_event(self, kind: str, data: dict) -> None:
        try:
            await Mongo.voice_events().insert_one({
                "session_id": self.session_id,
                "tenant_id": self.config.tenant_id,
                "bot_id": self.config.bot_id,
                "kind": kind,
                "at": datetime.now(timezone.utc),
                "data": data,
            })
        except Exception:  # noqa: BLE001 - persistence must not break the call
            logger.warning("voice event write failed (%s)", kind)

    async def finalize(self, reason: str = "completed") -> None:
        """Persist transcript + session summary. Runs exactly once: a second
        call (another teardown path, a duplicate hangup signal) is a no-op —
        the first reason stands and nothing is re-billed or re-enqueued."""
        if self._finalized:
            return
        self._finalized = True
        self.end_reason = reason
        duration = int(time.time() - self.started_at)
        if self._background_flushes:
            # Drain deferred event writes so nothing scheduled during the call
            # races the transcript write or is cancelled by teardown.
            await asyncio.gather(
                *list(self._background_flushes), return_exceptions=True
            )
        if self.recording_writer is not None:
            # Flush the audio tail and wrap the WAV — idempotent with the
            # pipeline's own stop hook, whichever fires first.
            await self.recording_writer.close()
        transcript = self.transcript_rows()
        try:
            payload = {
                "session_id": self.session_id,
                "control_plane_id": self.control_plane_id,
                "call_id": self.call_id,
                "tenant_id": self.config.tenant_id,
                "bot_id": self.config.bot_id,
                "channel": self.channel,
                "started_at": datetime.fromtimestamp(self.started_at, tz=timezone.utc),
                "duration_sec": duration,
                "end_reason": reason,
                "turns": transcript,
                "events": self.events,
                "usage": self.usage,
                "tts_usage": list(self.tts_usage.values()),
                "bot_version": self.config.version,
                # The engines this call was configured with (metrics: STT model
                # per call, decision model); per-turn TTS engines are on
                # turn_timing.tts_model, per-engine characters on tts_usage.
                "models": self.configured_models(),
                "language": self.language,
                "disposition": self.disposition,
                "customer_context_id": self.customer_context_id,
                "runtime_context_record_id": self.runtime_context_record_id,
                # Which published prompt version this call actually spoke from.
                "prompt_id": self.config.prompt_id or None,
                "prompt_version": self.config.prompt_version,
                "prompt_mode": self.config.prompt_mode or None,
            }
            if self.recording_info:
                payload["recording"] = self.recording_info
            if self.hangup:
                payload["hangup"] = self.hangup
            await Mongo.transcripts().update_one(
                {"session_id": self.session_id},
                {"$set": payload},
                upsert=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("transcript persistence failed for %s", self.session_id)

        await asyncio.to_thread(self._write_control_plane_row, duration, reason)

        # Tenant-scoped guardrail-trigger ledger (MySQL), written once —
        # includes the transcript-redaction hits recorded just above.
        if (
            self.guardrails is not None
            and self.guardrails.hits
            and not self._guardrail_triggers_persisted
        ):
            self._guardrail_triggers_persisted = True
            from shared.guardrails import persist_triggers_sync

            await asyncio.to_thread(
                persist_triggers_sync,
                list(self.guardrails.hits),
                tenant_id=self.config.tenant_id,
                bot_id=self.config.bot_id,
                session_id=self.session_id,
                channel=self.channel,
                effective=self.guardrails.effective,
            )

        if self.customer_context_id and self.call_state:
            # Record verification/dispute/complaint/payment/callback state
            # back onto the customer context row (whitelisted fields only).
            from shared.customer_context import record_call_state_sync

            await asyncio.to_thread(
                record_call_state_sync,
                self.customer_context_id,
                last_call_id=self.session_id,
                **self.call_state,
            )
        if self.runtime_context_record_id and self.call_state:
            # Same state, generic path: merged into the record's call_state
            # JSON — never into the tenant-owned data payload.
            from shared.runtime_context import record_context_call_state

            await record_context_call_state(
                self.runtime_context_record_id,
                {**self.call_state, "last_call_id": self.session_id},
            )

        # Durable post-call intelligence: enqueue the (idempotent) analysis
        # job AFTER everything above is persisted so the processor always
        # finds the transcript. The heavy work runs in the background worker
        # — teardown is never delayed, and a repeated finalize/hangup finds
        # the existing row and does nothing.
        try:
            from shared.post_call.processor import (
                enqueue_post_call,
                notify_post_call_worker,
            )

            enqueued = await asyncio.to_thread(enqueue_post_call, self)
            if enqueued:
                notify_post_call_worker()
        except Exception:  # noqa: BLE001 — teardown must never fail on this
            logger.exception("post-call enqueue failed for %s", self.session_id)

    def _write_control_plane_row(self, duration: int, reason: str) -> None:
        from decimal import Decimal

        from shared.billing.metering import record_usage_event, rollup_call
        from shared.db.mysql import get_sessionmaker
        from shared.models import ConversationSession

        session = get_sessionmaker()()
        try:
            existing = session.get(ConversationSession, self.control_plane_id)
            if existing is not None:
                return  # finalize already persisted this call — never re-bill
            escalated = any(e.get("kind") == "handoff" for e in self.events)
            row = ConversationSession(
                id=self.control_plane_id,
                # The link to this call's usage events — without it the stored
                # cost cannot be audited or recomputed later.
                session_id=self.session_id,
                call_id=self.call_id,
                tenant_id=self.config.tenant_id,
                bot_id=self.config.bot_id,
                channel="voice" if self.channel != "browser" else "web",
                caller_masked=mask_pii(self.caller or "", kinds={"phone"}) or None,
                started_at=datetime.fromtimestamp(self.started_at, tz=timezone.utc),
                duration_sec=duration,
                contained=not escalated,
                escalation_reason="human_handoff" if escalated else None,
                language=self.language,
                status="completed",
                disposition=self.disposition,
                prompt_id=self.config.prompt_id or None,
                prompt_version=self.config.prompt_version,
            )
            session.add(row)

            # Every billable component of the call, telephony included: the
            # stored total must equal the sum of this call's usage events or the
            # list view and the auditable breakdown disagree.
            row.cost_usd = self._record_usage_events(
                session, record_usage_event, duration
            )

            occurred = datetime.fromtimestamp(self.started_at, tz=timezone.utc)
            rollup_call(
                session,
                tenant_id=self.config.tenant_id,
                bot_id=self.config.bot_id,
                day=occurred.date(),
                contained=not escalated,
                escalated=escalated,
                minutes=Decimal(duration) / 60,
            )
            session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("conversation_sessions/usage write failed")
            session.rollback()
        finally:
            session.close()

    def _record_usage_events(self, session, record_usage_event, duration: int):
        """One usage event per capability/engine for this call (idempotent).

        Streaming chunks and turns were already aggregated in ``self.usage``;
        the deterministic request ids make a re-run of finalize a no-op.
        """
        from decimal import Decimal

        occurred = datetime.fromtimestamp(self.started_at, tz=timezone.utc).replace(tzinfo=None)
        total = Decimal(0)

        def _record(**kwargs):
            nonlocal total
            # Mock providers make no external call — nothing billable happened.
            if (kwargs.get("provider_code") or "") == "mock":
                return None
            event = record_usage_event(
                session,
                tenant_id=self.config.tenant_id,
                bot_id=self.config.bot_id,
                session_id=self.session_id,
                occurred_at=occurred,
                commit=False,
                **kwargs,
            )
            return event

        usage = self.usage
        llm_conf = self.config.llm or {}
        if usage.get("llm_requests") or usage.get("llm_output_tokens"):
            event = _record(
                capability="llm",
                provider_code=llm_conf.get("provider") or "openai",
                model_code=llm_conf.get("model") or "",
                request_id=f"{self.session_id}:llm",
                requests=int(usage.get("llm_requests") or 1),
                input_tokens=int(usage.get("llm_input_tokens") or 0),
                output_tokens=int(usage.get("llm_output_tokens") or 0),
                cached_tokens=int(usage.get("llm_cached_tokens") or 0),
                reasoning_tokens=int(usage.get("llm_reasoning_tokens") or 0),
                usage_source="estimated" if usage.get("llm_usage_estimated") else "provider",
            )
            if event is not None:
                total += Decimal(str(event.cost_usd))
        if usage.get("decision_llm_requests"):
            # The decision model usually differs from the conversation model
            # (platform default gpt-6-luna). Priced at the model's standard
            # rates: the fast-tier premium is not modelled in provider_pricing.
            decision_conf = llm_conf.get("orchestration") or {}
            event = _record(
                capability="llm",
                provider_code=decision_conf.get("provider") or llm_conf.get("provider") or "openai",
                model_code=decision_conf.get("model") or llm_conf.get("model") or "",
                request_id=f"{self.session_id}:llm-decision",
                requests=int(usage.get("decision_llm_requests") or 1),
                input_tokens=int(usage.get("decision_llm_input_tokens") or 0),
                output_tokens=int(usage.get("decision_llm_output_tokens") or 0),
                cached_tokens=int(usage.get("decision_llm_cached_tokens") or 0),
                usage_source="provider",
            )
            if event is not None:
                total += Decimal(str(event.cost_usd))

        stt_conf = self.config.stt or {}
        stt_seconds = self._stt_seconds
        stt_source, stt_basis = "provider", self._stt_basis
        if stt_seconds <= 0 and any(t.role == "user" for t in self.turns):
            # Fallback of last resort: transcripts exist but no final ever
            # carried a usable duration (and no PCM was measured). Bill the
            # connection duration and mark the event clearly as estimated.
            stt_seconds = Decimal(duration)
            stt_source, stt_basis = "estimated", "connection_duration"
            logger.warning(
                "stt usage fallback for %s: no provider-reported audio "
                "duration; billing connection duration (%ss)",
                self.session_id, duration,
            )
        if stt_seconds > 0:
            # Mirrored into the usage dict so the transcript document carries
            # the same figure the usage event bills.
            self.usage["stt_characters"] = self.stt_transcript_characters()
            event = _record(
                capability="stt",
                provider_code=stt_conf.get("provider") or "sarvam",
                model_code=stt_conf.get("model") or "",
                request_id=f"{self.session_id}:stt",
                requests=int(usage.get("stt_requests") or 1),
                audio_seconds=stt_seconds.quantize(Decimal("0.001")),
                characters=self.stt_transcript_characters(),
                usage_source=stt_source,
                usage_metadata={"basis": stt_basis} if stt_basis else None,
            )
            if event is not None:
                total += Decimal(str(event.cost_usd))

        for entry in self.tts_usage.values():
            key = f"{entry['provider']}|{entry['model']}|{entry['voice']}"
            event = _record(
                capability="tts",
                provider_code=entry["provider"] or "sarvam",
                model_code=entry["model"] or "",
                voice_code=entry["voice"] or None,
                request_id=f"{self.session_id}:tts:{key}"[:120],
                requests=int(entry["requests"] or 1),
                characters=int(entry["characters"] or 0),
            )
            if event is not None:
                total += Decimal(str(event.cost_usd))

        if self.channel not in ("browser", "web") and duration > 0:
            # Telephony minutes are metered on the connection duration (not on
            # speech), and are folded into the same total as AI usage — a
            # self-hosted trunk simply prices to zero. Excluding them here made
            # the stored conversation total silently differ from the sum of its
            # own usage events the moment a telephony rate was configured.
            event = _record(
                capability="telephony",
                provider_code=self.channel,
                model_code="",
                request_id=f"{self.session_id}:telephony",
                audio_seconds=duration,
                usage_metadata={"direction": "inbound"},
            )
            if event is not None:
                total += Decimal(str(event.cost_usd))

        return total


class CallRecordingWriter:
    """Streams the call's merged audio to disk and registers the finished WAV.

    Fed by an AudioBufferProcessor at the end of the pipeline (stereo: caller
    left, bot right). Chunks are appended to a ``.pcm.part`` file off the audio
    path (thread executor); on stop the raw PCM is wrapped into a WAV next to
    it and the reference is handed to the SessionRecorder, which persists it on
    the transcript document. Any failure disables the writer for the rest of
    the call — recording must never break audio.
    """

    def __init__(self, recorder: SessionRecorder) -> None:
        from shared.config import get_settings

        self._recorder = recorder
        self._relative = f"{recorder.config.tenant_id}/{recorder.session_id}.wav"
        self._wav_path = Path(get_settings().voice_recordings_dir) / self._relative
        self._part_path = self._wav_path.with_suffix(".pcm.part")
        self._sample_rate = 0
        self._channels = 1
        self._bytes = 0
        self._failed = False
        self._finalized = False
        # Serializes appends against finalize — the buffer processor delivers
        # events as detached tasks, so ordering is enforced here.
        self._lock = asyncio.Lock()
        # The AudioBufferProcessor feeding this writer; used by close() to
        # flush its tail buffer, since pipeline teardown does not reliably
        # deliver Cancel/End frames past the output transport.
        self.audiobuffer = None

    def _append_sync(self, audio: bytes) -> None:
        self._part_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._part_path, "ab") as fh:
            fh.write(audio)

    async def append(self, audio: bytes, sample_rate: int, num_channels: int) -> None:
        async with self._lock:
            if self._failed or self._finalized or not audio:
                return
            self._sample_rate = sample_rate
            self._channels = num_channels
            try:
                await asyncio.to_thread(self._append_sync, audio)
                self._bytes += len(audio)
            except Exception:  # noqa: BLE001
                self._failed = True
                logger.warning("call recording write failed for %s — recording disabled",
                               self._recorder.session_id, exc_info=True)

    def _wrap_wav_sync(self) -> None:
        with wave.open(str(self._wav_path), "wb") as wav:
            wav.setnchannels(self._channels)
            wav.setsampwidth(2)
            wav.setframerate(self._sample_rate)
            with open(self._part_path, "rb") as raw:
                while chunk := raw.read(1 << 20):
                    wav.writeframes(chunk)
        self._part_path.unlink(missing_ok=True)

    async def close(self) -> None:
        """Flush the processor's tail audio, then finalize the WAV.

        Called by SessionRecorder.finalize() at call end (the authoritative
        path); the processor's own on_recording_stopped hook finalizes too
        when a graceful EndFrame reaches it first — both are idempotent.
        """
        buffer = self.audiobuffer
        if buffer is not None:
            try:
                # Emits the remaining buffered audio through on_audio_data and
                # fires on_recording_stopped. Handlers are pinned to sync
                # dispatch in the pipeline, so this awaits the actual writes.
                await buffer.stop_recording()
            except Exception:  # noqa: BLE001
                logger.warning("audio buffer stop failed for %s",
                               self._recorder.session_id, exc_info=True)
        await self.finalize()

    async def finalize(self) -> None:
        """Wrap the streamed PCM into a WAV. Idempotent."""
        async with self._lock:
            if self._finalized:
                return
            self._finalized = True
            if self._failed or self._bytes == 0 or self._sample_rate <= 0:
                await asyncio.to_thread(self._part_path.unlink, missing_ok=True)
                return
            try:
                await asyncio.to_thread(self._wrap_wav_sync)
                frame_bytes = 2 * self._channels
                duration = self._bytes / (self._sample_rate * frame_bytes)
                self._recorder.set_recording(
                    path=self._relative,
                    duration_sec=duration,
                    sample_rate=self._sample_rate,
                    channels=self._channels,
                    size_bytes=self._wav_path.stat().st_size,
                )
                logger.info("call recording saved for %s (%.1fs)",
                            self._recorder.session_id, duration)
            except Exception:  # noqa: BLE001
                logger.warning("call recording finalize failed for %s",
                               self._recorder.session_id, exc_info=True)


class AlignedStereoRecorder:
    """Stereo call audio on ONE wall-clock timeline (caller left, bot right).

    The bot track is the far end's playout, rebuilt from what was actually
    written to the wire: the final outbound mix (reply, latency cues and the
    background ambience) in the packets the far end received, each placed
    where the far end plays it — queued behind the audio it still has, or,
    when it has run dry, at the moment the packet arrives. A clear
    (barge-in) drops what the far end had not played yet. So a gap in the
    recording is a gap the caller heard, and nothing else is.

    The caller track is the caller stream laid end to end. Its sample count
    is the caller's clock; the arrival time of a frame only bounds it (a
    frame cannot arrive before it was captured), which fixes the timeline's
    origin. Frames that reach the recorder late — it sits at the end of the
    pipeline, behind every processor and behind any event-loop stall — are
    delivery jitter, not missing audio: they are never padded for. Silence is
    inserted only for a gap that PERSISTS (the stream really lost audio), at
    the position where it opened. Audio the caller gate REPLAYS when it opens
    (its pre-roll: the onset of the caller's words, whose time it had already
    passed on as silence) goes back over that silence; it is not new time.

    The previous version appended the gate's replayed pre-roll after the
    silence it stood for (≈ 0.2 s more caller audio than wall time at every
    gate opening), padded the caller track whenever a frame arrived more than
    200 ms after the one before it and then appended the delayed frames as
    well, and laid bot audio onto the caller's ARRIVAL clock. Each of those
    lengthened the recording and cut a hole of the same size into whatever
    the bot was saying — a break the caller never heard — and the room
    sound, mixed in at the wire, was not recorded at all.

    Pure logic (no pipecat, no I/O): the frame plumbing lives in
    ``voice_runtime.pipeline.AlignedRecordingProcessor``.
    """

    # A caller frame this much behind the wall clock may be a real gap …
    GAP_MIN_S = 0.2
    # … if it is still that far behind after this long. A delayed burst
    # catches up within a few milliseconds per frame.
    GAP_CONFIRM_S = 0.5
    # Lateness shrinking by more than this is a burst catching up.
    GAP_CATCHUP_S = 0.03
    # An origin correction of at least this much re-times buffered bot audio;
    # anything smaller is clock drift (see _retime).
    RETIME_MIN_S = 0.005
    # No caller audio at all for this long while bot audio keeps going out:
    # the caller track is silence-filled so the recording keeps streaming.
    ABSENT_CALLER_S = 10.0
    # How the far end restarts once it has run dry: a telephony leg plays
    # from its next 20 ms tick, the browser client schedules 40 ms ahead
    # (src/services/voiceClient.ts PcmPlaybackQueue).
    FAR_END = {
        "telephony": {"tick_s": 0.02, "lead_s": 0.0},
        "browser": {"tick_s": 0.0, "lead_s": 0.04},
    }

    def __init__(self, *, sample_rate: int, chunk_seconds: float = 10.0,
                 max_bot_backlog_seconds: float = 120.0,
                 far_end: str = "telephony") -> None:
        self._rate = int(sample_rate)
        self._chunk_bytes = int(self._rate * 2 * chunk_seconds)
        model = self.FAR_END.get(far_end, self.FAR_END["telephony"])
        self._tick = int(round(self._rate * model["tick_s"]))
        self._lead = int(round(self._rate * model["lead_s"]))
        self._backlog_cap = int(self._rate * max_bot_backlog_seconds)
        self._absent_caller = int(self._rate * self.ABSENT_CALLER_S)
        # Both buffers start at timeline sample ``_base`` (already emitted).
        self._base = 0
        self._user = bytearray()
        self._bot = bytearray()
        self._user_end = 0            # timeline sample after the last caller sample
        self._user_started = False
        self._playhead = 0            # timeline sample where the far end runs dry
        self._origin: float | None = None   # monotonic time of timeline sample 0
        self._gap: list | None = None       # a possible caller-stream gap (see _track_gap)
        self._replay = bytearray()          # gate pre-roll awaiting its place
        self._stopped = False

    @property
    def sample_rate(self) -> int:
        return self._rate

    @staticmethod
    def _interleave(left: bytes, right: bytes) -> bytes:
        out = bytearray(len(left) * 2)
        out[0::4] = left[0::2]
        out[1::4] = left[1::2]
        out[2::4] = right[0::2]
        out[3::4] = right[1::2]
        return bytes(out)

    def _position(self, now: float) -> int:
        """Timeline sample being played/captured at wall time ``now``."""
        return max(0, int(round((now - self._origin) * self._rate)))

    # ── caller track ─────────────────────────────────────────────────────
    def add_user_audio(self, audio: bytes, *, now: float | None = None,
                       replay: bool = False) -> bytes:
        """Fold one caller frame in; returns a ready stereo chunk (or b"").

        ``replay``: the frame is audio the caller gate held back and is
        releasing now (its pre-roll). Its time span — the stretch just
        before the next live frame — is already on the track as the silence
        the gate passed on meanwhile, so it replaces that silence.
        """
        if self._stopped or len(audio) < 2:
            return b""
        now = time.monotonic() if now is None else now
        if len(audio) % 2:
            audio = audio[:-1]
        if replay:
            self._replay.extend(audio)
            return b""
        self._place_replay()
        samples = len(audio) // 2
        if self._origin is None:
            self._origin = now - samples / self._rate
        # How far this frame trails the wall clock. It can never lead it: a
        # negative value means the origin estimate was late (the frames that
        # set it had been delayed), so the origin moves back.
        late = (now - self._origin) - (self._user_end + samples) / self._rate
        if late < 0:
            self._retime(-late)
            late = 0.0
        if not self._user_started:
            self._user_started = True
            # The caller stream began this long after the first bot audio.
            self._pad_user(self._user_end, late)
        else:
            self._track_gap(late, now)
        self._user.extend(audio)
        self._user_end += samples
        return self._take_chunk()

    def _retime(self, seconds: float) -> None:
        """The timeline's origin was ``seconds`` later than assumed.

        Bot audio is placed by wall time, so what is still in the buffer was
        put that much too early: it moves with the origin. This happens as a
        call starts — its first caller frames reach the recorder late, in a
        burst, while the first outbound audio is already going out — and it
        keeps the two tracks together should the caller track ever run
        ahead of the clock. Sub-frame corrections are clock drift between
        the caller's sample clock and this host: the origin follows, the
        audio stays where it is (moving it would cut a sample into speech).
        """
        self._origin -= seconds
        samples = int(round(seconds * self._rate))
        if seconds < self.RETIME_MIN_S or samples <= 0 or self._playhead <= self._base:
            return
        self._bot[0:0] = bytes(samples * 2)
        self._playhead += samples

    def _place_replay(self) -> None:
        """Write the released pre-roll over the end of the caller track."""
        if not self._replay:
            return
        size = min(len(self._replay), len(self._user))    # what is not emitted yet
        if size:
            self._user[len(self._user) - size:] = self._replay[len(self._replay) - size:]
        self._replay.clear()

    def _track_gap(self, late: float, now: float) -> None:
        if late < self.GAP_MIN_S:
            self._gap = None
            return
        if self._gap is None:
            # [opened at, lateness then, position, smallest lateness since]
            self._gap = [now, late, self._user_end, late]
            return
        since, at_open, position, smallest = self._gap
        if late < at_open - self.GAP_CATCHUP_S:
            # Catching up: these are delayed frames, not a hole. Watch on.
            self._gap = [now, late, position, late]
            return
        smallest = min(smallest, late)
        self._gap[3] = smallest
        if now - since >= self.GAP_CONFIRM_S:
            # Still this far behind: the stream lost audio where it opened.
            # The smallest lateness seen is the hole; the rest was jitter.
            self._gap = None
            self._pad_user(position, smallest)

    def _pad_user(self, position: int, seconds: float) -> None:
        """Silence in the caller track at timeline sample ``position``."""
        samples = int(round(seconds * self._rate))
        if samples <= 0:
            return
        offset = max(0, min(len(self._user), (position - self._base) * 2))
        self._user[offset:offset] = bytes(samples * 2)
        self._user_end += samples

    # ── bot track (far-end playout) ──────────────────────────────────────
    def add_bot_audio(self, audio: bytes, *, now: float | None = None,
                      tolerance_s: float = 0.0) -> bytes:
        """Fold in audio that was sent to the far end at ``now``; returns a
        ready stereo chunk (or b"").

        ``tolerance_s``: how precisely ``now`` is known. The wire tap reports
        the send itself (0). A caller that only sees the frame after the
        transport's pacing wait passes its estimate and the slack, and audio
        within that slack of the playhead counts as continuous.
        """
        if self._stopped or len(audio) < 2:
            return b""
        now = time.monotonic() if now is None else now
        if len(audio) % 2:
            audio = audio[:-1]
        if self._origin is None:
            self._origin = now
        current = self._position(now)
        if self._playhead > current - int(tolerance_s * self._rate):
            start = self._playhead            # queued behind what is playing
        else:
            start = current + self._lead      # ran dry: playback restarts
            if self._tick:
                start = -(-start // self._tick) * self._tick
        start = max(start, self._base)        # what was emitted is final
        if start - current <= self._backlog_cap:
            self._write_bot(start, audio)
        if not self._user_started or current - self._user_end > self._absent_caller:
            self._fill_absent_caller(current)
        return self._take_chunk()

    def _write_bot(self, start: int, audio: bytes) -> None:
        offset = (start - self._base) * 2
        if offset < 0:                        # never: the past is final
            audio, offset = audio[-offset:], 0
        if len(self._bot) < offset:
            self._bot.extend(bytes(offset - len(self._bot)))
        self._bot[offset:offset + len(audio)] = audio
        self._playhead = start + len(audio) // 2

    def flush_bot(self, *, now: float | None = None) -> None:
        """The far end dropped the audio it had not played yet (barge-in
        ``killAudio`` / ``clear``, the browser client's stop)."""
        if self._stopped or self._origin is None:
            return
        current = self._position(time.monotonic() if now is None else now)
        if self._playhead <= current:
            return
        del self._bot[max(0, (current - self._base) * 2):]
        self._playhead = current

    def _fill_absent_caller(self, current: int) -> None:
        """No caller stream (yet, or any more) while bot audio goes out."""
        lag = current - self._user_end - self._absent_caller // 2
        if lag > 0 and current - self._user_end > self._absent_caller:
            self._gap = None
            self._pad_user(self._user_end, lag / self._rate)

    # ── output ───────────────────────────────────────────────────────────
    def _take_chunk(self, *, force: bool = False) -> bytes:
        size = len(self._user)
        if not force and (size < self._chunk_bytes or self._gap is not None):
            return b""
        if size <= 0:
            return b""
        if len(self._bot) < size:
            self._bot.extend(bytes(size - len(self._bot)))
        chunk = self._interleave(bytes(self._user[:size]), bytes(self._bot[:size]))
        del self._user[:size]
        del self._bot[:size]
        self._base += size // 2
        return chunk

    def stop(self, *, now: float | None = None) -> bytes:
        """Final chunk: everything up to the end of the call — the later of
        the caller stream's end and the bot audio the far end had played by
        now (audio still queued there was never heard). Idempotent."""
        if self._stopped:
            return b""
        self._stopped = True
        self._gap = None
        self._place_replay()
        end = self._user_end
        if self._origin is not None:
            current = self._position(time.monotonic() if now is None else now)
            end = max(end, min(self._playhead, current))
        del self._bot[max(0, (end - self._base) * 2):]
        if self._user_end < end:
            self._user.extend(bytes((end - self._user_end) * 2))
            self._user_end = end
        return self._take_chunk(force=True)
