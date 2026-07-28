"""Turn router — decides how to handle each caller utterance BEFORE any
expensive work happens. Priority order (per product spec):

1. Active deterministic workflow state
2. Explicit call-control commands (hangup / transfer / repeat / slower)
3. Known configured intents (keyword samples from bot config)
4. Configured tool/API mapping
5. Lightweight KB-retrieval decision (domain heuristics)
6. Default: plain LLM conversation; low-confidence → one clarification

The design (domain-word gating, skip-lists for smalltalk) is carried over from
the legacy VoiceBot rag_router/intent_engine, simplified and made stateless.
"""

import re
from dataclasses import dataclass, field
from enum import Enum


class RouteKind(str, Enum):
    WORKFLOW = "workflow"
    CALL_CONTROL = "call_control"
    INTENT = "intent"
    TOOL = "tool"
    KNOWLEDGE = "knowledge"
    CHAT = "chat"
    CLARIFY = "clarify"
    HANDOFF = "handoff"
    SAFETY = "safety"


@dataclass
class RouteDecision:
    kind: RouteKind
    confidence: float = 1.0
    reason: str = ""
    action: str | None = None  # hangup | transfer | repeat | slower | ...
    intent: str | None = None
    considered_kb: bool = False


_SMALLTALK = re.compile(
    r"^\s*(hi|hii+|hello|hey|good (morning|afternoon|evening)|namaste|"
    r"thanks?( you)?( so much)?|thank you|ok(ay)?|yes|yeah|no|nope|sure|great|"
    r"bye|goodbye|see you|talk (to you )?later)( there| everyone| all)?\s*[.!?]*\s*$",
    re.IGNORECASE,
)

# ── multilingual hang-up detection ──────────────────────────────────────────
# Deterministic, transcription-tolerant matching for Hindi (Devanagari),
# Hinglish (Latin) and English. Checked before EVERYTHING else (including an
# active workflow) — a caller asking to hang up must never receive another
# payment pitch, clarification or LLM fallback.
#
# NOTE: Python's \b misfires after Devanagari matra-final words — Devanagari
# alternates stay outside \b groups.

# Negations must never hang up: "फोन मत काटो", "call mat kato", "don't hang
# up". Both orders are covered (neg before verb, and "kaatna mat").
_HANGUP_NEGATION = re.compile(
    r"(?:\bmat\b|\bna\b|\bnahin?\b|\bdon'?t\b|\bdo not\b|मत|ना|नहीं)\W*"
    r"(?:\w+\W+)?(?:kat+\w*|kaat\w*|cut|band|bandh|khat[ae]?m|rakh\w*|hang|"
    r"disconnect|काट\w*|कट|बंद|ख़?त्म|रख)"
    r"|(?:kat+n[aei]|kaatn[aei]|काटना|कट करना)\W+(?:mat\b|मत)",
    re.I,
)

_HANGUP_PATTERNS: list[re.Pattern] = [
    # English.
    re.compile(
        r"\b(hang ?up|end (the |this )?call|disconnect( the| this)?( call| phone)?|"
        r"(cut|stop|drop) (the |this )?call)\b",
        re.I,
    ),
    # phone/call + cut/band/khatam/rakh verb (Latin and Devanagari nouns).
    re.compile(
        r"(?:\b(?:phone|phon|fone|call|kaal)\b|फ़?ोन|फ़ोन|फोन|कॉल|काल)\W*(?:ko\W+|को\W*)?"
        r"(?:kat+\w*|kaat\w*|cut|band\w*|khat[ae]?m|khatm|rakh\w*|काट\w*|कट|बंद|ख़?त्म|रख)",
        re.I,
    ),
    # Bare imperative cut verb: "cut kar do", "cut karo", "cut karu",
    # "kaat do", "काट दो", "कट करो". Past tense ("paise kat gaye" — money got
    # deducted) deliberately does NOT match: only imperative aux verbs listed.
    re.compile(
        r"\b(?:kat+|kaat|cut)\s+(?:kar\w*|kr\w*|do|de|dijiye|dena)\b",
        re.I,
    ),
    re.compile(r"(?:काट|कट)\s*(?:कर\s*)?(?:दो|दे|दीजिए|करो|करिए)"),
    # band/khatam without a phone/call noun needs a "bas" style terminator so
    # "SMS band karo" (stop the messages) can't kill the call.
    re.compile(
        r"\bbas\b.{0,16}\b(?:band|bandh|khat[ae]?m|khatm)\s+kar\w*",
        re.I,
    ),
    re.compile(r"बस.{0,16}(?:बंद|ख़?त्म)\s*कर"),
]


def detect_hangup(text: str) -> bool:
    """Deterministic multilingual hang-up intent (hi / hinglish / en)."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if _HANGUP_NEGATION.search(stripped):
        return False
    return any(p.search(stripped) for p in _HANGUP_PATTERNS)


_CALL_CONTROL: list[tuple[re.Pattern, str]] = [
    # hang-up lives in detect_hangup() (multilingual + negation-guarded),
    # checked before this list ever runs.
    (re.compile(r"\b(transfer|connect) (me )?(to )?(a |an )?(human|agent|person|representative|someone)\b", re.I), "transfer"),
    (re.compile(r"\b(speak|talk) (to|with) (a |an )?(human|agent|person|representative)\b", re.I), "transfer"),
    (re.compile(r"\b(repeat|say (that|it) again|pardon|come again)\b", re.I), "repeat"),
    (re.compile(r"\b(speak|talk|go) (more )?slow(ly|er)?\b", re.I), "slower"),
]

_HANDOFF = re.compile(r"\b(human|agent|supervisor|manager|representative)\b", re.I)

# Question shapes that usually need tenant knowledge.
_KB_SIGNALS = re.compile(
    r"\b(what|how|when|where|which|why|can i|do you|is there|are there|"
    r"policy|policies|coverage|premium|claim|deadline|grace period|charges?|"
    r"fees?|interest|rate|document|procedure|process|eligib|terms?|"
    r"conditions?|renewal|refund|cancel(lation)?|timings?|hours|address)\b",
    re.I,
)

_UNSAFE = re.compile(
    r"\b(card number|cvv|otp|one[- ]time password|password) (is|was)?\s*[:\-]?\s*\d",
    re.I,
)


class TurnRouter:
    """Stateless per-bot router; bot configuration is passed per call."""

    def __init__(
        self,
        *,
        intents: list[dict] | None = None,
        kb_keywords: list[str] | None = None,
        has_knowledge_bases: bool = True,
        workflows: dict[str, str] | None = None,
    ) -> None:
        self._intents = intents or []
        self._kb_extra = re.compile(
            r"\b(" + "|".join(re.escape(k) for k in kb_keywords) + r")\b", re.I
        ) if kb_keywords else None
        self._has_kbs = has_knowledge_bases
        self._workflows = workflows or {}

    def decide(self, text: str, *, active_workflow: str | None = None) -> RouteDecision:
        stripped = (text or "").strip()
        if not stripped:
            return RouteDecision(kind=RouteKind.CLARIFY, confidence=0.3, reason="empty_input")

        # 0a. Hang-up outranks everything, including an active workflow: a
        # caller asking to end the call must never get another pitch, rung,
        # clarification or LLM fallback (any language).
        if detect_hangup(stripped):
            return RouteDecision(kind=RouteKind.CALL_CONTROL, action="hangup",
                                 reason="hangup_phrase")

        # 0. Safety: caller reading out secrets — refuse/deflect, never store.
        if _UNSAFE.search(stripped):
            return RouteDecision(kind=RouteKind.SAFETY, reason="sensitive_disclosure")

        # 1. An active workflow consumes the turn (slot filling).
        if active_workflow:
            # Explicit escape hatches still win inside a workflow.
            for pattern, action in _CALL_CONTROL:
                if pattern.search(stripped):
                    if action == "transfer":
                        return RouteDecision(kind=RouteKind.HANDOFF, action="transfer",
                                             reason="transfer_in_workflow")
                    return RouteDecision(
                        kind=RouteKind.CALL_CONTROL, action=action, reason="call_control_in_workflow"
                    )
            return RouteDecision(
                kind=RouteKind.WORKFLOW, reason=f"active_workflow:{active_workflow}"
            )

        # 2. Call control.
        for pattern, action in _CALL_CONTROL:
            if pattern.search(stripped):
                if action == "transfer":
                    return RouteDecision(kind=RouteKind.HANDOFF, action="transfer",
                                         reason="explicit_transfer_request")
                return RouteDecision(kind=RouteKind.CALL_CONTROL, action=action,
                                     reason="call_control_command")
        if _HANDOFF.search(stripped) and re.search(r"\b(want|need|give|get)\b", stripped, re.I):
            return RouteDecision(kind=RouteKind.HANDOFF, action="transfer", reason="handoff_phrase")

        # 3. Smalltalk never hits the knowledge base.
        if _SMALLTALK.match(stripped):
            return RouteDecision(kind=RouteKind.CHAT, reason="smalltalk", considered_kb=True)

        # 4. Configured intents (sample keyword voting).
        intent = self._match_intent(stripped)
        if intent is not None:
            name, route, confidence = intent
            if route and route.startswith("workflow:"):
                return RouteDecision(kind=RouteKind.WORKFLOW, intent=name, confidence=confidence,
                                     action=route.split(":", 1)[1], reason="intent_workflow")
            if route and route.startswith("tool:"):
                return RouteDecision(kind=RouteKind.TOOL, intent=name, confidence=confidence,
                                     action=route.split(":", 1)[1], reason="intent_tool")
            # Explicit destination routes: "knowledge" forces tenant-safe KB
            # retrieval (needed for locales the _KB_SIGNALS heuristics don't
            # cover), "handoff" escalates to a human agent deterministically.
            if route == "knowledge" and self._has_kbs:
                return RouteDecision(kind=RouteKind.KNOWLEDGE, intent=name, confidence=confidence,
                                     reason="intent_knowledge", considered_kb=True)
            if route == "handoff":
                return RouteDecision(kind=RouteKind.HANDOFF, intent=name, confidence=confidence,
                                     action="transfer", reason="intent_handoff")
            # Semantic hang-up: tenant-configured sample phrases (any language)
            # escalate to the same deterministic hang-up flow.
            if route == "hangup":
                return RouteDecision(kind=RouteKind.CALL_CONTROL, intent=name,
                                     confidence=confidence, action="hangup",
                                     reason="intent_hangup")
            return RouteDecision(kind=RouteKind.INTENT, intent=name, confidence=confidence,
                                 reason="configured_intent")

        # 5. Knowledge decision — question-shaped + domain terms + KBs exist.
        if self._has_kbs:
            kb_hit = bool(_KB_SIGNALS.search(stripped)) or bool(
                self._kb_extra and self._kb_extra.search(stripped)
            )
            wordish = len(stripped.split()) >= 3
            if kb_hit and wordish:
                return RouteDecision(kind=RouteKind.KNOWLEDGE, confidence=0.8,
                                     reason="kb_signals", considered_kb=True)

        # 6. Very short, ambiguous input → one concise clarification.
        if len(stripped.split()) <= 2:
            return RouteDecision(kind=RouteKind.CLARIFY, confidence=0.4, reason="too_short",
                                 considered_kb=True)

        return RouteDecision(kind=RouteKind.CHAT, confidence=0.6, reason="default_chat",
                             considered_kb=self._has_kbs)

    def _match_intent(self, text: str) -> tuple[str, str | None, float] | None:
        lowered = text.lower()
        best: tuple[str, str | None, float] | None = None
        for intent in self._intents:
            samples = [s.lower() for s in (intent.get("samples") or [])]
            if not samples:
                continue
            hits = sum(1 for s in samples if s and s in lowered)
            score = hits / len(samples) if samples else 0.0
            threshold = float(intent.get("confidence_threshold") or 0.5)
            # A single exact sample phrase match is a strong signal.
            if hits and (score >= threshold or any(s == lowered for s in samples)):
                confidence = max(score, 0.9 if any(s == lowered for s in samples) else score)
                if best is None or confidence > best[2]:
                    best = (intent.get("name", "intent"), intent.get("route"), confidence)
        return best
