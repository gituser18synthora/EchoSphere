"""Identifier-collection mode: session state while a workflow awaits a
numeric identifier (order id, phone, booking/account/policy/claim number…).

Activated GENERICALLY from the workflow's currently awaited field schema —
the engine reports ``awaitingIdentifier`` whenever its paused node is an ask
whose entity expects digits (see workflow_engine). Nothing here keys on bot,
tenant or workflow identity, and accepted lengths/validation come from the
same entity configuration the workflow's own matcher uses.

While active, the brain:
- buffers digit-fragment STT finals in memory instead of dispatching a turn
  (and a TTS acknowledgement) per fragment,
- waits a tolerant inter-digit pause window between fragments (ordinary
  conversation endpoints are untouched),
- dispatches IMMEDIATELY when the accumulated candidate satisfies the
  configured matcher exactly,
- retains the utterance's post-gate PCM (bounded) so one batch transcription
  can recover an identifier the streaming STT mangled,
- skips the Goal Engine for digit-dominant turns — the deterministic workflow
  outranks LLM scope guesses.

The capture is bounded by time, digit count and audio bytes, and is cleared
on completion, workflow change, hang-up and session cleanup. Digit VALUES
live only in this in-memory object; events log counts, never numbers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from shared.orchestration.entity_extractor import (
    extract_entity,
    identifier_length_bounds,
)
from shared.orchestration.spoken_numbers import (
    digits_dominant,
    spoken_digit_sequence,
)

# Inter-digit pause tolerance while a caller dictates one digit at a time.
# Real callers may pause for 2–3 seconds to read the next group (and a jittery
# browser uplink can widen the server-observed gap). Exact valid identifiers
# still dispatch immediately, so the more patient default only affects an
# incomplete numeric sequence and prevents the bot from reading back a partial
# ID while the caller is still dictating it. Configurable per bot via
# stt_settings.identifier_pause_window; bounded so a typo cannot stall turns.
DEFAULT_PAUSE_WINDOW_SECONDS = 3.0
MIN_PAUSE_WINDOW_SECONDS = 0.6
MAX_PAUSE_WINDOW_SECONDS = 4.0

# The whole collection episode is time-bounded: past this, fragments dispatch
# on the normal endpoints again (the workflow's retry ladder takes over).
MAX_CAPTURE_SECONDS = 120.0


def resolve_pause_window(stt_settings: dict | None) -> float:
    """The configured identifier inter-digit window, safely bounded."""
    raw = (stt_settings or {}).get("identifier_pause_window")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_PAUSE_WINDOW_SECONDS
    return min(MAX_PAUSE_WINDOW_SECONDS, max(MIN_PAUSE_WINDOW_SECONDS, value))


@dataclass
class IdentifierCapture:
    """Live state for one workflow ask node collecting a numeric identifier."""

    workflow: str
    node: str
    variable: str
    entity: dict
    held_digits: str = ""  # digits the WORKFLOW already holds for this node
    pause_window: float = DEFAULT_PAUSE_WINDOW_SECONDS
    started_at: float = field(default_factory=time.monotonic)
    min_digits: int = 0
    max_digits: int = 0
    # One batch-audio recovery per capture episode, only when streaming left
    # the candidate invalid (never an extra call on the happy path).
    recovery_attempted: bool = False

    def __post_init__(self) -> None:
        self.min_digits, self.max_digits = identifier_length_bounds(self.entity)

    @classmethod
    def from_awaiting(
        cls, workflow: str, payload: dict, *, pause_window: float
    ) -> "IdentifierCapture":
        return cls(
            workflow=workflow,
            node=str(payload.get("node") or ""),
            variable=str(payload.get("variable") or ""),
            entity=dict(payload.get("entity") or {}),
            held_digits=str(payload.get("held_digits") or ""),
            pause_window=pause_window,
        )

    def refresh(self, payload: dict) -> None:
        """Fold the engine's post-turn view back in (held digits change)."""
        self.held_digits = str(payload.get("held_digits") or "")

    def expired(self) -> bool:
        return time.monotonic() - self.started_at > MAX_CAPTURE_SECONDS

    # ── candidate evaluation ────────────────────────────────────────────

    def candidate(self, buffered_text: str) -> str:
        """Workflow-held digits + the digits of the brain's pending buffer."""
        return (self.held_digits + spoken_digit_sequence(buffered_text or ""))

    def is_dictation(self, text: str) -> bool:
        return digits_dominant(text or "")

    def matches(self, digits: str) -> bool:
        """Whether the candidate satisfies the SAME authoritative matcher the
        workflow's ask node uses (regex/lexicon/builtin, via the entity)."""
        if not digits:
            return False
        return bool(extract_entity(digits, dict(self.entity)).get("matched"))

    def overflowed(self, digits: str) -> bool:
        return len(digits) > self.max_digits

    def hold_delay(self, buffered_text: str) -> float | None:
        """How long to wait before dispatching the buffered dictation.

        0.0   → dispatch now (exact match, overflow, or episode expired);
        window → keep waiting for more digits;
        None  → not a dictation turn; normal endpoints apply.
        """
        if not self.is_dictation(buffered_text):
            return None
        digits = self.candidate(buffered_text)
        if not digits:
            return None
        if self.matches(digits) or self.overflowed(digits) or self.expired():
            return 0.0
        return self.pause_window
