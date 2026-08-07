"""Validated structure of one call's post-call intelligence.

The analysis LLM's output is never trusted raw: everything is clamped,
bounded and normalized here before persistence, mirroring the conventions of
:mod:`shared.orchestration.decision_schema`. Hard rules the schema enforces:

- a commitment's ``due_date`` survives only as an ABSOLUTE ISO date — a raw
  relative expression ("Monday", "कल") is resolved against the call time by
  :mod:`shared.post_call.dates`, or dropped to None with the raw expression
  preserved for audit;
- a payment can never be *verified* by the analysis: ``"verified"`` is not a
  commitment status this schema accepts (verification is a backend/tool fact
  recorded during the call, not a summary opinion);
- confidence is clamped to [0, 1]; unknown enum-ish values degrade to safe
  defaults instead of failing the whole record;
- free text is length-bounded so a runaway generation cannot bloat storage.
"""

from datetime import date, datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.post_call.dates import resolve_relative_date

_MAX_SUMMARY_CHARS = 1200
_MAX_ITEM_CHARS = 160
_MAX_LIST_ITEMS = 12
_MAX_SLOT_VALUE_CHARS = 120

# Commitment lifecycle the platform tracks across calls. "verified" is
# deliberately absent — only a real backend verification (recorded as a tool
# result during a call) may claim that, never a summary.
COMMITMENT_STATUSES = ("promised", "open", "kept", "broken", "cancelled")

NBA_PRIORITIES = ("low", "medium", "high", "urgent")

# Platform Next-Best-Action vocabulary. Domain-neutral by construction: what
# each action MEANS operationally is up to the tenant's campaign tooling; a
# bot's goal policy may extend this set (BotGoalPolicy via
# goal_policy.next_actions) with its own action names.
PLATFORM_NEXT_ACTIONS = (
    "follow_up_later",
    "follow_up_on_commitment",
    "retry_commitment",
    "verify_previous_payment",
    "collect_missing_information",
    "schedule_callback",
    "escalate_to_human",
    "close_goal_completed",
    "do_not_contact",
    "continue_pending_workflow",
    "no_action",
)


def _clean_text(value, limit: int) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return " ".join(str(value).split())[:limit]


def _clean_list(value, *, limit: int = _MAX_LIST_ITEMS) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = _clean_text(item, _MAX_ITEM_CHARS)
        if text and text not in out:
            out.append(text)
    return out[:limit]


class CustomerCommitment(BaseModel):
    """One concrete commitment the customer made ("I will pay ₹2,000 Monday").

    ``type`` is tenant vocabulary (payment, appointment, document, callback…)
    — shared code never interprets it beyond storage and follow-up timing.
    """

    model_config = ConfigDict(extra="ignore")

    type: str = "commitment"
    description: str = ""
    amount: float | None = None
    currency: str = ""
    due_date: date | None = None
    # What the customer actually said about the date, preserved for audit even
    # when it resolved to an absolute due_date.
    raw_due_expression: str = ""
    status: str = "promised"

    @field_validator("type", "currency", mode="before")
    @classmethod
    def _clean_short(cls, v):
        return _clean_text(v, 40)

    @field_validator("description", "raw_due_expression", mode="before")
    @classmethod
    def _clean_desc(cls, v):
        return _clean_text(v, _MAX_ITEM_CHARS)

    @field_validator("amount", mode="before")
    @classmethod
    def _clean_amount(cls, v):
        try:
            if v is None or isinstance(v, bool):
                return None
            value = float(str(v).replace(",", ""))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @field_validator("due_date", mode="before")
    @classmethod
    def _clean_date(cls, v):
        if v is None or isinstance(v, date):
            return v
        text = str(v).strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            # Not ISO: leave for resolve_dates() to attempt from the raw
            # expression; the string itself is not a date.
            return None

    @field_validator("status", mode="before")
    @classmethod
    def _clamp_status(cls, v):
        v = str(v or "").strip().lower()
        # An analysis claiming "verified"/"completed" degrades to the honest
        # open state — verification is a tool fact, not a summary opinion.
        return v if v in COMMITMENT_STATUSES else "promised"

    def is_open(self) -> bool:
        return self.status in ("promised", "open")


class NextBestAction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str = "follow_up_later"
    reason: str = ""
    priority: str = "medium"
    recommended_at: datetime | None = None
    # Runtime bookkeeping: which layer decided the final action.
    source: str = "llm"  # llm | rules | fallback

    @field_validator("action", mode="before")
    @classmethod
    def _clean_action(cls, v):
        return _clean_text(v, 60).lower().replace(" ", "_") or "follow_up_later"

    @field_validator("reason", mode="before")
    @classmethod
    def _clean_reason(cls, v):
        return _clean_text(v, 300)

    @field_validator("priority", mode="before")
    @classmethod
    def _clamp_priority(cls, v):
        v = str(v or "").strip().lower()
        return v if v in NBA_PRIORITIES else "medium"

    @field_validator("recommended_at", mode="before")
    @classmethod
    def _clean_when(cls, v):
        if v is None or isinstance(v, datetime):
            return v
        text = str(v).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed


class PostCallAnalysis(BaseModel):
    """The complete validated post-call record for one conversation."""

    model_config = ConfigDict(extra="ignore")

    # Set by the processor, never by the model.
    conversation_id: str = ""

    call_outcome: str = ""
    summary: str = ""
    customer_intent: str = ""
    customer_sentiment: str = ""
    customer_commitments: list[CustomerCommitment] = Field(default_factory=list)
    objections: list[str] = Field(default_factory=list)
    important_facts: list[str] = Field(default_factory=list)
    resolved_items: list[str] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)
    collected_slots: dict[str, str] = Field(default_factory=dict)
    missing_slots: list[str] = Field(default_factory=list)
    next_best_action: NextBestAction = Field(default_factory=NextBestAction)
    follow_up_required: bool = True
    confidence: float = 0.0
    # Language continuity for the next call (platform locale forms).
    dominant_language: str = ""
    last_customer_language: str = ""
    # Runtime bookkeeping.
    source: str = "llm"  # llm | fallback

    @field_validator(
        "call_outcome", "customer_intent", "customer_sentiment", mode="before"
    )
    @classmethod
    def _clean_labels(cls, v):
        return _clean_text(v, 60).lower().replace(" ", "_")

    @field_validator("summary", mode="before")
    @classmethod
    def _clean_summary(cls, v):
        return _clean_text(v, _MAX_SUMMARY_CHARS)

    @field_validator(
        "objections", "important_facts", "resolved_items", "unresolved_items",
        "missing_slots", mode="before",
    )
    @classmethod
    def _clean_lists(cls, v):
        return _clean_list(v)

    @field_validator("collected_slots", mode="before")
    @classmethod
    def _clean_slots(cls, v):
        if not isinstance(v, dict):
            return {}
        out: dict[str, str] = {}
        for key, value in list(v.items())[:24]:
            name = _clean_text(key, 64)
            if not name or value is None or isinstance(value, (dict, list)):
                continue
            out[name] = _clean_text(value, _MAX_SLOT_VALUE_CHARS)
        return out

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v):
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.0

    @field_validator("dominant_language", "last_customer_language", mode="before")
    @classmethod
    def _clean_language(cls, v):
        return _clean_text(v, 15)

    @field_validator("customer_commitments", mode="before")
    @classmethod
    def _coerce_commitments(cls, v):
        if not isinstance(v, (list, tuple)):
            return []
        out: list[dict] = []
        for item in v:
            if not isinstance(item, dict):
                continue
            entry = dict(item)
            for camel, snake in (
                ("dueDate", "due_date"),
                ("rawDueExpression", "raw_due_expression"),
                ("date", "due_date"),  # spec-style commitments use "date"
            ):
                if camel in entry and snake not in entry:
                    entry[snake] = entry[camel]
            out.append(entry)
        return out[:8]

    @model_validator(mode="after")
    def _consistency(self):
        # An open commitment or unresolved items imply follow-up unless the
        # NBA explicitly says the contact must not be called again.
        if self.next_best_action.action in ("do_not_contact", "no_action",
                                            "close_goal_completed"):
            self.follow_up_required = False
        elif any(c.is_open() for c in self.customer_commitments):
            self.follow_up_required = True
        return self

    def resolve_dates(self, *, reference: datetime) -> None:
        """Resolve any commitment whose date arrived as a relative expression.

        The LLM is instructed to emit ISO dates; when it left ``due_date``
        empty but captured the spoken expression, the deterministic resolver
        anchors it to the call's own time.
        """
        for commitment in self.customer_commitments:
            if commitment.due_date is None and commitment.raw_due_expression:
                commitment.due_date = resolve_relative_date(
                    commitment.raw_due_expression, reference=reference
                )
            if commitment.due_date is None and commitment.description:
                commitment.due_date = resolve_relative_date(
                    commitment.description, reference=reference
                )

    def memory_payload(self) -> dict:
        """JSON-serializable structured memory for persistence."""
        payload = self.model_dump(mode="json")
        payload.pop("conversation_id", None)
        return payload


def parse_analysis(raw: object) -> PostCallAnalysis | None:
    """Validate one raw model payload, or None so the caller can fall back."""
    if not isinstance(raw, dict):
        return None
    # Tolerate camelCase model output for the common fields.
    aliases = {
        "callOutcome": "call_outcome",
        "customerIntent": "customer_intent",
        "customerSentiment": "customer_sentiment",
        "customerCommitments": "customer_commitments",
        "importantFacts": "important_facts",
        "resolvedItems": "resolved_items",
        "unresolvedItems": "unresolved_items",
        "collectedSlots": "collected_slots",
        "missingSlots": "missing_slots",
        "nextBestAction": "next_best_action",
        "followUpRequired": "follow_up_required",
        "dominantLanguage": "dominant_language",
        "lastCustomerLanguage": "last_customer_language",
    }
    data = dict(raw)
    for camel, snake in aliases.items():
        if camel in data and snake not in data:
            data[snake] = data[camel]
    nba = data.get("next_best_action")
    if isinstance(nba, dict):
        for camel, snake in (("recommendedAt", "recommended_at"),):
            if camel in nba and snake not in nba:
                nba[snake] = nba[camel]
    if not any(key in data for key in ("summary", "call_outcome", "next_best_action")):
        return None
    try:
        return PostCallAnalysis.model_validate(data)
    except Exception:  # noqa: BLE001 — malformed output falls back, never raises
        return None
