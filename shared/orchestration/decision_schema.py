"""Structured conversation decisions — the contract between understanding and speech.

Stage A of every orchestrated turn produces ONE validated
:class:`ConversationDecision`; Stage B (response generation) and every state
transition run strictly AFTER that decision has been validated. The schema is
domain-neutral by construction: nothing in it knows about loans, patients or
policies — domain meaning arrives through the bot's configured goals
(shared.orchestration.goal_engine), never through shared Python constants.

Hard rules the schema itself enforces (so no caller can forget them):

- a slot value exists ONLY when the caller actually provided one
  (``status == "provided"``). "हाँ, नंबर है" arrives as ``exists_claimed`` and
  carries NO value — saying a value exists is never the value;
- an out-of-scope or injection-attempt turn can never request a tool and is
  forced onto a redirect action — a caller cannot steer the bot off its
  configured goal by asking nicely (or by asking it to ignore its rules);
- confidence is clamped to [0, 1]; unknown enum values degrade to safe
  defaults instead of raising mid-call;
- ``parse_decision`` returns None for output that carries no recognizable
  decision content at all, so the caller falls back to its deterministic
  path instead of acting on noise.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Identity/confirmation outcomes a decision may report for a pending question.
DECISION_OUTCOMES = (
    "confirmed", "denied", "ambiguous", "unrelated", "needs_clarification",
)

# Scope classification of the caller's utterance against the bot's goals.
SCOPE_IN = "in_scope"
SCOPE_OUT = "out_of_scope"
SCOPE_INJECTION = "injection_attempt"
SCOPE_RESULTS = (SCOPE_IN, SCOPE_OUT, SCOPE_INJECTION)

# What the caller's slot-related utterance actually established.
SLOT_STATUSES = ("provided", "exists_claimed", "unavailable", "refused", "unclear")

# The validated next-action vocabulary. The runtime maps these onto its own
# guarded transitions — an action outside this list never routes anywhere.
NEXT_ACTIONS = (
    "continue_workflow",
    "ask_identity_confirmation",
    "request_slot_value",
    "clarify",
    "answer",
    "answer_from_knowledge",
    "redirect_to_goal",
    "call_tool",
    "escalate_to_human",
    "end_call",
)

_MAX_RESPONSE_CHARS = 600
_MAX_SLOT_VALUE_CHARS = 120
_MAX_REASON_CHARS = 300


class SlotObservation(BaseModel):
    """One slot as the caller's LAST utterance affected it.

    ``value`` survives validation only for ``status == "provided"`` — every
    other status states a fact ABOUT the value ("I have it", "I don't have
    it", refusal, noise) without supplying one, and downstream code must ask
    for the actual value instead of pretending it was captured.
    """

    model_config = ConfigDict(extra="ignore")

    status: str = "unclear"
    value: str | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _clamp_status(cls, v):
        v = str(v or "").strip().lower()
        return v if v in SLOT_STATUSES else "unclear"

    @field_validator("value", mode="before")
    @classmethod
    def _clean_value(cls, v):
        if v is None or isinstance(v, (dict, list)):
            return None
        text = str(v).strip()
        return text[:_MAX_SLOT_VALUE_CHARS] or None

    @model_validator(mode="after")
    def _value_requires_provided(self):
        if self.status != "provided":
            self.value = None
        elif self.value is None:
            # A "provided" claim with no value is not a provided value.
            self.status = "unclear"
        return self


class ConversationDecision(BaseModel):
    """One validated Stage-A decision for one completed caller turn."""

    model_config = ConfigDict(extra="ignore")

    # Business intent (from the bot's configured list) and generic
    # conversation signal (platform vocabulary) — either may be None.
    intent: str | None = None
    signal: str | None = None
    # Outcome of the question the bot was waiting on (identity confirmation,
    # a yes/no gate): None when no such question was pending or answered.
    decision: str | None = None
    scope: str = SCOPE_IN
    confidence: float = 0.0
    reason: str = ""
    slots: dict[str, SlotObservation] = Field(default_factory=dict)
    next_action: str = "answer"
    # Advisory only: which configured tool the model believes applies. The
    # runtime binds tools from intent CONFIGURATION and the backend-validated
    # executor — this field never executes anything by itself.
    tool_request: str | None = None
    needs_clarification: bool = False
    # Optional co-generated reply. Spoken ONLY when the runtime's direct-speech
    # gate allows it (validated decision, no tool/knowledge/workflow involved).
    response_text: str = ""

    # Conversation language, carried explicitly (never model-supplied): the
    # runtime stamps what the caller spoke THIS turn (STT detection + script/
    # lexicon agreement) and the language the reply must be generated and
    # synthesized in. Response generation and TTS follow response_language.
    user_language: str = ""      # platform locale, e.g. "hi-IN" / "en-IN"
    response_language: str = ""

    # Runtime bookkeeping (never model-supplied).
    source: str = "llm"          # llm | fallback
    latency_ms: float = 0.0

    @field_validator("intent", "signal", "tool_request", mode="before")
    @classmethod
    def _clean_name(cls, v):
        if v is None:
            return None
        text = str(v).strip()
        return text[:80] or None

    @field_validator("decision", mode="before")
    @classmethod
    def _clamp_decision(cls, v):
        if v is None:
            return None
        v = str(v).strip().lower()
        return v if v in DECISION_OUTCOMES else None

    @field_validator("scope", mode="before")
    @classmethod
    def _clamp_scope(cls, v):
        v = str(v or "").strip().lower()
        # A misread scope must not hijack the turn into a redirect: default in.
        return v if v in SCOPE_RESULTS else SCOPE_IN

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v):
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.0

    @field_validator("reason", mode="before")
    @classmethod
    def _clean_reason(cls, v):
        return str(v or "").strip()[:_MAX_REASON_CHARS]

    @field_validator("next_action", mode="before")
    @classmethod
    def _clamp_action(cls, v):
        v = str(v or "").strip().lower()
        return v if v in NEXT_ACTIONS else "answer"

    @field_validator("response_text", mode="before")
    @classmethod
    def _clean_response(cls, v):
        if v is None or isinstance(v, (dict, list)):
            return ""
        return str(v).strip()[:_MAX_RESPONSE_CHARS]

    @field_validator("slots", mode="before")
    @classmethod
    def _coerce_slots(cls, v):
        if not isinstance(v, dict):
            return {}
        out: dict[str, dict] = {}
        for key, entry in list(v.items())[:16]:
            name = str(key).strip()[:64]
            if not name:
                continue
            if isinstance(entry, dict):
                out[name] = entry
            elif entry is None:
                out[name] = {"status": "unclear"}
            else:
                # A bare value means the caller supplied it.
                out[name] = {"status": "provided", "value": entry}
        return out

    @model_validator(mode="after")
    def _enforce_scope_rules(self):
        """Deterministic guardrails no model output can bypass."""
        if self.scope != SCOPE_IN:
            # Off-goal turns never call tools, never fill slots, never
            # confirm a pending gate and never advance a workflow — they
            # redirect (or escalate/end when the model judged that necessary).
            self.tool_request = None
            self.slots = {}
            if self.decision is not None:
                self.decision = "unrelated"
            if self.next_action not in ("escalate_to_human", "end_call"):
                self.next_action = "redirect_to_goal"
        if self.decision in ("denied", "ambiguous", "needs_clarification"):
            # A denied/unclear gate answer can never simultaneously complete
            # anything: keep the conversation on the pending question.
            if self.next_action in ("continue_workflow", "end_call", "call_tool"):
                self.next_action = (
                    "ask_identity_confirmation"
                    if self.intent == "identity_confirmation"
                    else "clarify"
                )
        return self

    def provided_values(self) -> dict[str, str]:
        """Slot values the caller ACTUALLY provided this turn."""
        return {
            name: obs.value
            for name, obs in self.slots.items()
            if obs.status == "provided" and obs.value
        }

    def as_event(self) -> dict:
        """Log-safe summary: slot statuses only — never slot values."""
        return {
            "intent": self.intent,
            "signal": self.signal,
            "decision": self.decision,
            "scope": self.scope,
            "confidence": round(self.confidence, 3),
            "reason": self.reason[:160],
            "slots": {name: obs.status for name, obs in self.slots.items()},
            "next_action": self.next_action,
            "tool_request": self.tool_request,
            "needs_clarification": self.needs_clarification,
            "has_response_text": bool(self.response_text),
            "user_language": self.user_language or None,
            "response_language": self.response_language or None,
            "source": self.source,
            "latency_ms": round(self.latency_ms, 1),
        }


# Keys that make a raw model payload recognizable as a decision at all.
_MEANINGFUL_KEYS = (
    "intent", "signal", "decision", "scope", "next_action", "nextAction",
    "slots", "entities", "response_text", "responseText", "needs_clarification",
)


def parse_decision(
    raw: object,
    *,
    allowed_intents: set[str] | frozenset[str] = frozenset(),
    allowed_signals: tuple[str, ...] | set[str] = (),
) -> ConversationDecision | None:
    """Validate one raw model payload into a decision, or None to fall back.

    Tolerant on shape (camelCase aliases, entities-as-slots) but strict on
    meaning: an intent outside the configured list is discarded (a made-up
    intent name must never route anywhere), a platform signal placed in the
    intent slot is recovered as the signal, and a payload with none of the
    recognizable keys is rejected outright.
    """
    if not isinstance(raw, dict):
        return None
    if not any(key in raw for key in _MEANINGFUL_KEYS):
        return None

    data = dict(raw)
    # camelCase aliases in model output stay accepted.
    for camel, snake in (
        ("nextAction", "next_action"),
        ("responseText", "response_text"),
        ("needsClarification", "needs_clarification"),
        ("toolRequest", "tool_request"),
    ):
        if camel in data and snake not in data:
            data[snake] = data[camel]
    # Classifier-style "entities" fold into slots as provided values.
    entities = data.get("entities")
    if isinstance(entities, dict) and not isinstance(data.get("slots"), dict):
        data["slots"] = {
            key: {"status": "provided", "value": value}
            for key, value in entities.items()
            if value is not None and not isinstance(value, (dict, list))
        }

    try:
        decision = ConversationDecision.model_validate(data)
    except Exception:  # noqa: BLE001 — malformed output falls back, never raises
        return None

    signals = set(allowed_signals)
    if decision.intent and decision.intent not in allowed_intents:
        # The model may put a platform signal in the intent slot — accept it
        # as the signal; anything else unknown is discarded.
        if decision.intent in signals and not decision.signal:
            decision.signal = decision.intent
        decision.intent = None
    if decision.signal and signals and decision.signal not in signals:
        decision.signal = None
    return decision
