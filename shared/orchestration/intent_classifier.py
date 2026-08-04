"""Hybrid business-intent pipeline: LLM understanding, deterministic edges.

Ordering per the platform spec:

1. Platform-critical commands (hang-up, transfer, do-not-call, emergency)
   are detected DETERMINISTICALLY in shared.orchestration.router — before
   this module ever runs, and never through an LLM.
2. Tenant sample phrases run as a fast path (a tenant-configurable
   optimization): an exact configured phrase answers in ~0 ms with no model
   call.
3. Otherwise a small, bounded LLM call classifies the FINAL user turn into
   the tenant's configured intents + generic conversation signals, and
   extracts the intent's entities — multilingual by construction (the model
   reads Hindi/Hinglish/English natively) instead of by regex accretion.
4. Confidence gates the result: below the intent's threshold the pipeline
   reports low confidence and the caller is asked to clarify, not guessed at.
5. On LLM failure or timeout the legacy regex signals
   (router.classify_user_signal) are the deterministic fallback, so a
   provider outage degrades understanding quality without dropping turns.

Tool selection is NEVER taken from the model: whether an intent requires a
tool, and which one, comes from the intent's configuration (route /
api_connection_id) — the model only says WHAT the caller meant; the
backend-validated executor decides what may happen next.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field

from shared.orchestration.router import classify_user_signal

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 2.0
_DEFAULT_THRESHOLD = 0.6
_MAX_HISTORY_TURNS = 4

# Generic conversation signals the platform (policy + workflow edges) reasons
# about. The LLM classifies into these when no tenant intent fits; the regex
# fallback produces the same vocabulary, so downstream code sees ONE language.
PLATFORM_SIGNALS = (
    "complaint", "clarify", "already_paid", "wrong_person", "agent_request",
    "hardship", "callback", "question", "refusal", "payment_intent", "affirm",
)

# Default entity slots for platform signals (tenant intents define their own).
_SIGNAL_ENTITIES = {
    "already_paid": ("payment_date", "payment_method", "transaction_reference"),
    "callback": ("callback_time",),
    "payment_intent": ("payment_amount", "payment_method", "payment_time"),
}

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


@dataclass
class IntentClassification:
    """One classified user turn — the contract the brain routes on."""

    intent: str | None = None          # tenant intent name (configured)
    signal: str | None = None          # platform conversation signal
    confidence: float = 0.0
    entities: dict = field(default_factory=dict)
    requires_tool: bool = False        # derived from intent CONFIG, not the LLM
    tool_name: str | None = None
    should_interrupt_current_flow: bool = False
    below_threshold: bool = False
    source: str = "none"               # llm | phrase | regex | none
    latency_ms: float = 0.0
    raw: dict | None = None

    def as_event(self) -> dict:
        return {
            "intent": self.intent, "signal": self.signal,
            "confidence": round(self.confidence, 3),
            "entities": {k: v for k, v in self.entities.items() if v is not None},
            "requires_tool": self.requires_tool, "tool": self.tool_name,
            "interrupts_flow": self.should_interrupt_current_flow,
            "below_threshold": self.below_threshold,
            "source": self.source, "latency_ms": round(self.latency_ms, 1),
        }


def _phrase_match(intents: list[dict], text: str) -> tuple[dict, float] | None:
    """Exact/near sample-phrase match — the tenant-configurable fast path.

    This gate decides whether to SKIP the LLM, so it is deliberately stricter
    than the router's own sample voting: an exact configured phrase, or a
    majority of samples hitting, is a confident match; one substring hit on a
    low-threshold intent is not — live calls showed a 0.06-score hit claiming
    a turn the model should have read. Weak matches fall through to the LLM
    (and the router still applies its own legacy voting for routing).
    """
    lowered = text.lower().strip()
    best: tuple[dict, float] | None = None
    for intent in intents:
        samples = [s.lower().strip() for s in (intent.get("samples") or []) if s]
        if not samples:
            continue
        if any(s == lowered for s in samples):
            return intent, 0.95
        hits = sum(1 for s in samples if s and s in lowered)
        score = hits / len(samples)
        threshold = max(float(intent.get("confidence_threshold") or 0.5), 0.5)
        if hits and score >= threshold and (best is None or score > best[1]):
            best = (intent, score)
    return best


class HybridIntentPipeline:
    """Per-call classifier bound to one bot's configured intents."""

    def __init__(
        self,
        *,
        llm=None,
        intents: list[dict] | None = None,
        enabled: bool = True,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        default_threshold: float = _DEFAULT_THRESHOLD,
    ) -> None:
        self._llm = llm
        self._intents = [i for i in (intents or []) if i.get("name")]
        self._enabled = enabled and llm is not None
        self._timeout = timeout_seconds
        self._default_threshold = default_threshold
        self._by_name = {i["name"]: i for i in self._intents}
        self._system_prompt: str | None = None
        # Token usage of the most recent LLM classification (input, output) —
        # the caller folds it into the call's billable LLM usage.
        self.last_usage: tuple[int, int] | None = None

    # ── public API ────────────────────────────────────────────────────────

    async def classify(
        self,
        text: str,
        history: list[dict] | None = None,
        *,
        active_workflow: str | None = None,
    ) -> IntentClassification:
        started = time.perf_counter()
        stripped = (text or "").strip()
        if not stripped:
            return IntentClassification(source="none")

        # 2. Tenant phrase fast path (optimization, not the understanding layer).
        matched = _phrase_match(self._intents, stripped)
        if matched is not None:
            intent, confidence = matched
            result = self._from_intent(intent, confidence, source="phrase")
            result.signal = classify_user_signal(stripped)
            result.latency_ms = (time.perf_counter() - started) * 1000
            return result

        # 3. LLM structured classification of the completed turn.
        if self._enabled:
            try:
                parsed = await asyncio.wait_for(
                    self._classify_llm(stripped, history or [], active_workflow),
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("intent classification timed out (%.1fs)", self._timeout)
                parsed = None
            except Exception:  # noqa: BLE001 — classification must degrade, not raise
                logger.exception("intent classification failed")
                parsed = None
            if parsed is not None:
                result = self._from_llm(parsed, stripped)
                result.latency_ms = (time.perf_counter() - started) * 1000
                return result

        # 5. Deterministic fallback: legacy regex signals.
        signal = classify_user_signal(stripped)
        result = IntentClassification(
            signal=signal,
            confidence=0.5 if signal else 0.0,
            source="regex",
        )
        if signal in self._by_name:
            # A tenant configured an intent with the same name as the signal.
            configured = self._from_intent(self._by_name[signal], 0.5, source="regex")
            configured.signal = signal
            result = configured
        result.latency_ms = (time.perf_counter() - started) * 1000
        return result

    # ── LLM path ──────────────────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        if self._system_prompt is not None:
            return self._system_prompt
        lines = [
            "You classify ONE caller utterance from a phone call. The caller "
            "may speak Hindi, English, or mixed Hinglish (Latin or Devanagari "
            "script).",
            "Reply with ONLY a JSON object, no prose, no code fences:",
            '{"intent": <string|null>, "signal": <string|null>, '
            '"confidence": <0..1>, "entities": {<key>: <string|null>}, '
            '"should_interrupt_current_flow": <bool>}',
            "",
            "intent: the business intent, ONLY from this configured list "
            "(null if none fits):",
        ]
        for intent in self._intents[:40]:
            samples = ", ".join(f'"{s}"' for s in (intent.get("samples") or [])[:3])
            entities = ", ".join(
                [*(intent.get("entities") or []), *(intent.get("optional_entities") or [])]
            )
            lines.append(
                f'- {intent["name"]}'
                + (f': {intent["description"]}' if intent.get("description") else "")
                + (f" (examples: {samples})" if samples else "")
                + (f" [entities: {entities}]" if entities else "")
            )
        lines += [
            "",
            "signal: the generic conversation signal, ONLY from: "
            + ", ".join(PLATFORM_SIGNALS) + " (null if none fits). "
            "Meanings: complaint = the caller says the BOT is not "
            "listening/repeating itself; clarify = the caller did not "
            "understand the bot; already_paid = claims a required payment "
            "was already made; wrong_person = wrong number or 'that is not "
            "me/my account'; agent_request = wants a human; hardship = says "
            "they cannot pay/afford or has a crisis; callback = busy now, "
            "call later; question = asks for information; refusal = declines "
            "what was asked; payment_intent = commits to pay/do the asked "
            "action; affirm = a bare yes/agreement.",
            "",
            "entities: extract values LITERALLY from the utterance for the "
            "matched intent's entity keys"
            + (
                " (for already_paid use payment_date, payment_method, "
                "transaction_reference; for callback use callback_time)"
            )
            + ". Use null when the caller did not say it. NEVER invent values.",
            "should_interrupt_current_flow: true when what the caller said "
            "must change the current script (a claim, dispute, complaint, "
            "refusal or emergency), false for answers that continue it.",
            "confidence: your certainty in the chosen intent/signal.",
        ]
        self._system_prompt = "\n".join(lines)
        return self._system_prompt

    async def _classify_llm(
        self, text: str, history: list[dict], active_workflow: str | None
    ) -> dict | None:
        recent = history[-_MAX_HISTORY_TURNS:]
        convo = "\n".join(
            f"{'Bot' if m.get('role') == 'assistant' else 'Caller'}: {m.get('content', '')}"
            for m in recent
        )
        user = (
            (f"Recent conversation:\n{convo}\n\n" if convo else "")
            + (f"(A scripted flow '{active_workflow}' is active.)\n" if active_workflow else "")
            + f"Classify the caller's latest utterance:\n{text}"
        )
        result = await self._llm.generate(
            [{"role": "user", "content": user}],
            system=self._build_system_prompt(),
            temperature=0.0,
            max_tokens=250,
        )
        self.last_usage = (
            int(getattr(result, "input_tokens", 0) or 0),
            int(getattr(result, "output_tokens", 0) or 0),
        )
        raw = (result.text or "").strip()
        match = _JSON_BLOCK.search(raw)
        if match is None:
            logger.warning("intent classifier returned no JSON: %r", raw[:120])
            return None
        try:
            parsed = json.loads(match.group(0))
        except ValueError:
            logger.warning("intent classifier returned invalid JSON: %r", raw[:120])
            return None
        return parsed if isinstance(parsed, dict) else None

    def _from_llm(self, parsed: dict, text: str) -> IntentClassification:
        intent_name = parsed.get("intent")
        intent_name = str(intent_name) if intent_name else None
        if intent_name is not None and intent_name not in self._by_name:
            # The model may put a platform signal in the intent slot — accept
            # it as the signal; anything else unknown is discarded (a made-up
            # intent name must never route anywhere).
            if intent_name in PLATFORM_SIGNALS and not parsed.get("signal"):
                parsed["signal"] = intent_name
            intent_name = None

        signal = parsed.get("signal")
        signal = str(signal) if signal in PLATFORM_SIGNALS else None

        try:
            confidence = max(0.0, min(1.0, float(parsed.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0

        entities_in = parsed.get("entities")
        entities: dict = {}
        expected: tuple | list = ()
        if intent_name is not None:
            configured = self._by_name[intent_name]
            expected = [
                *(configured.get("entities") or []),
                *(configured.get("optional_entities") or []),
            ]
        elif signal in _SIGNAL_ENTITIES:
            expected = _SIGNAL_ENTITIES[signal]
        if isinstance(entities_in, dict):
            for key in expected or entities_in.keys():
                value = entities_in.get(key)
                if value is not None and not isinstance(value, (dict, list)):
                    entities[str(key)] = str(value)
                else:
                    entities[str(key)] = None

        if intent_name is not None:
            result = self._from_intent(
                self._by_name[intent_name], confidence, source="llm"
            )
        else:
            result = IntentClassification(confidence=confidence, source="llm")
        result.signal = signal or (result.signal if result.intent else None)
        result.entities = entities
        result.should_interrupt_current_flow = bool(
            parsed.get("should_interrupt_current_flow")
        )
        result.raw = parsed
        if result.below_threshold and signal is None:
            # Low-confidence AND no generic signal: keep the regex opinion so
            # a weak LLM answer never loses to silence.
            result.signal = classify_user_signal(text)
        return result

    def _from_intent(
        self, intent: dict, confidence: float, *, source: str
    ) -> IntentClassification:
        threshold = float(
            intent.get("confidence_threshold") or self._default_threshold
        )
        route = intent.get("route") or ""
        tool_name = None
        if route.startswith("tool:"):
            tool_name = route.split(":", 1)[1]
        elif intent.get("api_connection_id"):
            tool_name = str(intent["api_connection_id"])
        return IntentClassification(
            intent=intent.get("name"),
            signal=intent.get("name") if intent.get("name") in PLATFORM_SIGNALS else None,
            confidence=confidence,
            requires_tool=tool_name is not None,
            tool_name=tool_name,
            below_threshold=confidence < threshold,
            source=source,
        )
