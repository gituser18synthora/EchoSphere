"""Human speech naturalness planning (the "how to say it" layer).

The LLM / goal engine decides *what* the bot says; this module decides *how*
it is delivered: occasional thinking fillers and acknowledgements, contextual
tool-lookup prefaces, sparse mid-caller-speech backchannels, per-sentence
pause/rate variation, and (optional, off by default) rare self-corrections.

Design constraints, in priority order:

* **Deterministic and cheap** — pure config + RNG, no model calls, so it can
  run inside the first-audio critical path.  A planner call is microseconds.
* **Contextual and probabilistic** — nothing is injected on every turn, and
  serious caller signals (complaint/hardship/…) suppress playful hesitation.
* **Critical-content safe** — segments carrying amounts, dates, identifiers,
  OTPs or compliance wording never receive fillers, corrections or ambiguous
  pacing (`contains_critical_content`).
* **Language + gender aware** — variant pools exist per base language; Hindi
  entries are authored in masculine first-person form and re-agreed through
  ``adapt_authored_speaker_grammar`` using the *active* catalog voice, so a
  bot whose fallback voice differs in gender stays grammatical.  Languages
  without a pool simply get no fillers (never cross-language fillers).

Configuration resolves platform defaults -> tenant override -> bot override
(``resolve_human_speech``); the merged dict rides ResolvedBotConfig.
"""

from __future__ import annotations

import random
import re
import time
from collections import deque
from dataclasses import dataclass, field

from .voice_identity import VoiceIdentity, adapt_authored_speaker_grammar

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

HUMAN_SPEECH_DEFAULTS: dict = {
    # Feature switches
    "enabled": True,
    "thinking_fillers": True,
    "acknowledgements": True,
    "backchannels": True,
    "prosody_variation": True,
    "gender_agreement": True,
    "micro_pauses": True,
    "self_correction": False,
    # Tunables (probabilities are per-opportunity, 0..1)
    "thinking_filler_probability": 0.25,
    "acknowledgement_probability": 0.4,
    "tool_ack_probability": 0.9,
    "backchannel_probability": 0.35,
    "micro_pause_probability": 0.45,
    "self_correction_probability": 0.01,
    "min_long_turn_for_backchannel_ms": 4000,
    "min_gap_between_backchannels_ms": 8000,
    "max_backchannels_per_call": 4,
}

_BOOL_KEYS = (
    "enabled", "thinking_fillers", "acknowledgements", "backchannels",
    "prosody_variation", "gender_agreement", "micro_pauses", "self_correction",
)
_PROBABILITY_KEYS = (
    "thinking_filler_probability", "acknowledgement_probability",
    "tool_ack_probability", "backchannel_probability",
    "micro_pause_probability", "self_correction_probability",
)
_INT_KEYS = {
    "min_long_turn_for_backchannel_ms": (1000, 60_000),
    "min_gap_between_backchannels_ms": (2000, 120_000),
    "max_backchannels_per_call": (0, 20),
}


def validate_human_speech(value: object) -> list[str]:
    """Strict validation for API-saved overrides (runtime merging is lenient).

    Returns a list of problems; empty means valid. Overrides are sparse —
    only overridden keys need to be present.
    """
    problems: list[str] = []
    if not isinstance(value, dict):
        return ["human_speech must be an object"]
    for key, item in value.items():
        if key in _BOOL_KEYS:
            if not isinstance(item, bool):
                problems.append(f"'{key}' must be a boolean")
        elif key in _PROBABILITY_KEYS:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                problems.append(f"'{key}' must be a number between 0 and 1")
            elif not 0.0 <= float(item) <= 1.0:
                problems.append(f"'{key}' must be between 0 and 1")
        elif key in _INT_KEYS:
            low, high = _INT_KEYS[key]
            if isinstance(item, bool) or not isinstance(item, int):
                problems.append(f"'{key}' must be an integer")
            elif not low <= item <= high:
                problems.append(f"'{key}' must be between {low} and {high}")
        else:
            problems.append(f"unknown key '{key}'")
    return problems


def resolve_human_speech(*layers: dict | None) -> dict:
    """Merge human-speech config layers (platform -> tenant -> bot).

    Later layers win per key.  Unknown keys are dropped and every value is
    clamped/coerced so a junk override can never break a live call.
    """
    merged = dict(HUMAN_SPEECH_DEFAULTS)
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        for key, value in layer.items():
            if key in _BOOL_KEYS:
                if isinstance(value, bool):
                    merged[key] = value
            elif key in _PROBABILITY_KEYS:
                try:
                    merged[key] = min(1.0, max(0.0, float(value)))
                except (TypeError, ValueError):
                    pass
            elif key in _INT_KEYS:
                low, high = _INT_KEYS[key]
                try:
                    merged[key] = min(high, max(low, int(value)))
                except (TypeError, ValueError):
                    pass
    return merged


# --------------------------------------------------------------------------
# Critical-content detection
# --------------------------------------------------------------------------

# Naturalness must never blur amounts, dates, identifiers, verification codes
# or compliance wording.  False positives are safe (a segment just loses its
# decoration), so these patterns are deliberately broad.
_CRITICAL_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\d{3,}"),                                  # amounts / ids / phones
    re.compile(r"\d+[.,]\d+"),                              # decimals / groupings
    re.compile(r"[₹$€£]"),
    re.compile(r"(?<!\w)(?:rs\.?|rupees?|rupay?e|inr|usd|eur)(?!\w)", re.IGNORECASE),
    re.compile(r"(?<!\w)(?:otp|pin|cvv)(?!\w)", re.IGNORECASE),
    re.compile(
        r"(?<!\w)(?:transaction|txn|utr|reference|ref\.?|account|a/c|khata|"
        r"verification|code|id)(?!\w)",
        re.IGNORECASE,
    ),
    # Dates: 12/08, 12-08-2026, "25 tareekh", month names (en + romanized hi)
    re.compile(r"\d{1,2}\s*[/-]\s*\d{1,2}"),
    re.compile(r"\d{1,2}\s*(?:tareekh|taareekh|taarikh|तारीख)", re.IGNORECASE),
    re.compile(
        r"(?<!\w)(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|"
        r"dec(?:ember)?|janvari|farvari|अगस्त|जनवरी|फरवरी|मार्च|अप्रैल|मई|जून|"
        r"जुलाई|सितंबर|अक्टूबर|नवंबर|दिसंबर)(?!\w)",
        re.IGNORECASE,
    ),
    # Devanagari digits
    re.compile(r"[०-९]"),
    # Verbalized amounts: LLM replies speak numbers as words ("पच्चीस हज़ार
    # रुपये", "two thousand rupees") — digits alone would miss them.
    re.compile(
        r"(?<!\w)(?:हज़ार|हजार|लाख|करोड़|सौ|hazaa?r|laakh|lakh|crore|"
        r"thousand|hundred|million)(?!\w)",
        re.IGNORECASE,
    ),
    # Compliance / consent wording
    re.compile(
        r"(?<!\w)(?:consent|recorded|recording|legal|notice|compliance|waiver|"
        r"sahmati|kanooni|कानूनी|सहमति|रिकॉर्ड)(?!\w)",
        re.IGNORECASE,
    ),
)


def contains_critical_content(text: str) -> bool:
    """True when ``text`` carries content whose clarity must not be reduced."""
    if not text:
        return False
    return any(pattern.search(text) for pattern in _CRITICAL_PATTERNS)


# --------------------------------------------------------------------------
# Variant pools
# --------------------------------------------------------------------------

# Hindi entries are authored in masculine first-person form; feminine forms
# are derived at selection time via adapt_authored_speaker_grammar so pools
# stay in lock-step with the catalog-driven identity logic.  Entries whose
# male/female adaptations differ are skipped for neutral-gender voices.
#
# Hinglish is Hindi here (same convention as transcript_gate / phrases.py).
_POOLS: dict[str, dict[str, tuple[str, ...]]] = {
    "hi": {
        # NOTE: "Ek minute..." deliberately absent here — announcing a wait
        # and then not looking anything up sounds broken; it lives in the
        # "checking" pool where a lookup actually follows.
        "thinking": ("Hmm...", "Achha...", "Dekhiye...", "Haan..."),
        "acknowledgement": ("Achha...", "Ji...", "Theek hai...", "Haan ji...",
                            "Ji, main samajh raha hoon..."),
        "checking": (
            "Ek minute, main check karta hoon...",
            "Achha... ek minute, main check karta hoon.",
            "Ji, ek second... main dekh raha hoon.",
            "Ek moment, main abhi dekhta hoon...",
        ),
        "empathy": (
            "Ji, main samajh sakta hoon.",
            "Hmm... ji, main samajh raha hoon.",
            "Ji...",
        ),
        "backchannel": ("hmm...", "ji...", "achha...", "haan..."),
        "correction_token": ("sorry", "maaf kijiye"),
    },
    "en": {
        "thinking": ("Hmm...", "Okay...", "Right...", "Let me see..."),
        "acknowledgement": ("Okay...", "Right...", "I see...", "Got it..."),
        "checking": (
            "One moment, let me check...",
            "Okay... give me a second, let me look that up.",
            "Right, let me check that for you...",
        ),
        "empathy": ("I understand.", "I hear you...", "Okay, I understand..."),
        "backchannel": ("hmm...", "right...", "okay..."),
        "correction_token": ("sorry",),
    },
}

# Caller signals (shared/orchestration/intent_classifier PLATFORM_SIGNALS)
# that mark a serious / distressed context: no playful hesitation, only a
# brief empathetic acknowledgement is permitted.
_SERIOUS_SIGNALS = frozenset(
    {"complaint", "hardship", "refusal", "wrong_person", "agent_request"}
)
# Signals where an acknowledgement feels natural before the answer.
_ACK_SIGNALS = frozenset({"affirm", "already_paid", "payment_intent", "callback"})

_QUESTION_END = re.compile(r"[?？]\s*$")
_WORD_RE = re.compile(r"\S+")


def base_language(locale: str | None) -> str:
    """Platform locale -> pool key ('hi-IN' -> 'hi'). Hinglish rides 'hi'."""
    code = (locale or "").strip().lower()
    if not code:
        return ""
    base = code.split("-", 1)[0].split("_", 1)[0]
    return "hi" if base in ("hi", "hinglish") else base


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------


@dataclass
class TurnSpeechPlan:
    """How the upcoming assistant turn should be delivered."""

    preface: str = ""
    preface_kind: str = ""          # thinking | acknowledgement | checking | empathy
    allow_self_correction: bool = False
    telemetry: dict = field(default_factory=dict)

    @property
    def has_preface(self) -> bool:
        return bool(self.preface)


@dataclass
class SegmentDelivery:
    """Delivery metadata for one already-aggregated TTS sentence."""

    pause_after_ms: int | None = None   # None -> router default
    speed_scale: float | None = None    # multiplier on the bot's base speed
    critical: bool = False


# --------------------------------------------------------------------------
# Planner
# --------------------------------------------------------------------------


class SpeechNaturalnessPlanner:
    """Config-driven, per-call naturalness planner.

    One instance is created per call and shared by the conversation brain
    (turn prefaces, backchannels, self-correction) and the TTS router
    (per-sentence pause/rate variation).  All methods are synchronous and
    allocation-light; everything is safe to call on the audio hot path.
    """

    def __init__(self, config: dict | None = None, *,
                 rng: random.Random | None = None) -> None:
        self._config = resolve_human_speech(config)
        self._rng = rng or random.Random()
        # Per-pool recent picks: never repeat the last two variants.
        self._recent: dict[str, deque[str]] = {}
        self._last_preface_turn: int | None = None
        self._last_backchannel_monotonic: float | None = None
        self._backchannels_played = 0

    # -- config ----------------------------------------------------------

    @property
    def config(self) -> dict:
        return dict(self._config)

    @property
    def enabled(self) -> bool:
        return bool(self._config["enabled"])

    @property
    def backchannels_enabled(self) -> bool:
        return self.enabled and bool(self._config["backchannels"])

    @property
    def min_long_turn_for_backchannel_ms(self) -> int:
        return int(self._config["min_long_turn_for_backchannel_ms"])

    @property
    def min_gap_between_backchannels_ms(self) -> int:
        return int(self._config["min_gap_between_backchannels_ms"])

    # -- variant selection -----------------------------------------------

    def _adapted(self, text: str, identity: VoiceIdentity | None) -> str:
        if not self._config["gender_agreement"]:
            return text
        if identity is None or identity.gender not in ("male", "female"):
            return text
        return adapt_authored_speaker_grammar(text, identity)

    @staticmethod
    def _is_gendered(text: str) -> bool:
        male = adapt_authored_speaker_grammar(text, VoiceIdentity(gender="male"))
        female = adapt_authored_speaker_grammar(text, VoiceIdentity(gender="female"))
        return male != female

    def _pick(self, language: str, pool_key: str,
              identity: VoiceIdentity | None) -> str:
        pool = _POOLS.get(language, {}).get(pool_key) or ()
        if not pool:
            return ""
        gender = identity.gender if identity else "neutral"
        candidates = [
            entry for entry in pool
            if gender in ("male", "female") or not self._is_gendered(entry)
        ]
        if not candidates:
            return ""
        recent = self._recent.setdefault(f"{language}:{pool_key}", deque(maxlen=2))
        fresh = [entry for entry in candidates if entry not in recent]
        choice = self._rng.choice(fresh or candidates)
        recent.append(choice)
        return self._adapted(choice, identity)

    # -- turn-level planning ----------------------------------------------

    def plan_turn(self, *, language: str, identity: VoiceIdentity | None = None,
                  signal: str = "", route_kind: str = "llm",
                  turn_index: int = 0) -> TurnSpeechPlan:
        """Plan delivery for the upcoming assistant turn.

        ``route_kind``: "tool" (a backend lookup runs before the reply),
        "kb" (knowledge retrieval), "llm" (generated reply), or "direct"
        (deterministic/scripted reply text).
        ``signal`` is the platform caller signal from the decision layer.
        """
        plan = TurnSpeechPlan()
        cfg = self._config
        lang = base_language(language)
        plan.telemetry = {
            "filler_used": False,
            "filler_type": "",
            "language": lang,
            "gender_mode": identity.gender if identity else "neutral",
            "signal": signal,
            "route_kind": route_kind,
        }
        if not self.enabled:
            plan.telemetry["suppression_reason"] = "disabled"
            return plan
        if lang not in _POOLS:
            # Unsupported pool language: never inject cross-language fillers.
            plan.telemetry["suppression_reason"] = f"no_pool_language:{lang or '?'}"
            return plan
        if turn_index <= 0:
            plan.telemetry["suppression_reason"] = "greeting_turn"
            return plan  # never decorate the greeting

        serious = signal in _SERIOUS_SIGNALS
        roll = self._rng.random()
        suppression = ""

        pool_key = ""
        if route_kind == "tool":
            # A lookup is about to run: a spoken "let me check" both sounds
            # human and masks tool latency. Serious contexts get the calmer
            # empathetic form first.
            if roll < cfg["tool_ack_probability"]:
                pool_key = "empathy" if serious else "checking"
            else:
                suppression = "roll"
        elif serious:
            # Distressed/annoyed caller: at most a brief empathetic
            # acknowledgement; never playful hesitation.
            if cfg["acknowledgements"] and roll < cfg["acknowledgement_probability"]:
                pool_key = "empathy"
            else:
                suppression = "serious_signal"
        else:
            # Acknowledgements are NOT gated on a platform signal being
            # present: the decision layer can time out (signal == ""), and a
            # statement deserves a "Ji..."/"Okay..." regardless of which
            # understanding path classified it. A recognized ack signal keeps
            # full probability; an unclassified statement gets a reduced one.
            ack_probability = cfg["acknowledgement_probability"] * (
                1.0 if signal in _ACK_SIGNALS else 0.6 if signal in ("", "question") else 0.0
            )
            if signal == "question":
                # Questions read better with a beat of thought than a flat ack.
                ack_probability = 0.0
            think_ok = cfg["thinking_fillers"] and route_kind in ("llm", "kb")
            if cfg["acknowledgements"] and roll < ack_probability:
                pool_key = "acknowledgement"
            elif think_ok and roll < cfg["thinking_filler_probability"]:
                pool_key = "thinking"
            else:
                suppression = "roll"

        # Anti-repetition: a preface on the immediately previous turn makes
        # another one much less likely (tool acks exempt — dead air is worse).
        if (
            pool_key
            and route_kind != "tool"
            and self._last_preface_turn == turn_index - 1
            and self._rng.random() > 0.35
        ):
            pool_key = ""
            suppression = "anti_repetition"

        if pool_key:
            preface = self._pick(lang, pool_key, identity)
            if preface:
                plan.preface = preface
                plan.preface_kind = pool_key
                self._last_preface_turn = turn_index
                suppression = ""
                # Kept in telemetry because the plan's preface field is
                # consumed (emptied) by the speaking path before events write.
                plan.telemetry["selected_filler"] = preface
            else:
                suppression = "no_pool_variant"
        plan.telemetry["suppression_reason"] = suppression

        plan.allow_self_correction = (
            bool(cfg["self_correction"]) and route_kind in ("llm", "direct")
            and not serious
        )
        plan.telemetry["filler_used"] = plan.has_preface
        plan.telemetry["filler_type"] = plan.preface_kind
        return plan

    # -- segment-level planning (TTS router) -------------------------------

    def plan_segment(self, text: str, *, base_pause_ms: int,
                     language: str = "") -> SegmentDelivery:
        """Per-sentence delivery: pause variation + subtle rate variation.

        Called by the TTS router for each aggregated sentence (pause mode).
        Critical segments get clear pacing and a slightly longer separating
        pause; questions slow down a touch; everything else may receive a
        small deterministic jitter so pacing never sounds metronomic.
        """
        delivery = SegmentDelivery()
        cfg = self._config
        if not self.enabled:
            return delivery

        delivery.critical = contains_critical_content(text)
        is_question = bool(_QUESTION_END.search(text or ""))
        words = len(_WORD_RE.findall(text or ""))

        if cfg["prosody_variation"]:
            if delivery.critical:
                # Clear pacing for amounts/dates/ids: slightly slower, never
                # faster, no jitter.
                delivery.speed_scale = 0.96
            elif is_question:
                delivery.speed_scale = round(0.95 + self._rng.random() * 0.03, 3)
            else:
                delivery.speed_scale = round(0.97 + self._rng.random() * 0.06, 3)

        if cfg["micro_pauses"] and base_pause_ms > 0:
            if delivery.critical:
                # A clean boundary around critical content aids comprehension.
                delivery.pause_after_ms = min(700, base_pause_ms + 120)
            elif words <= 3:
                # Short ack fragments ("Achha...") read best with a beat after.
                delivery.pause_after_ms = min(700, max(180, base_pause_ms + 70))
            elif self._rng.random() < cfg["micro_pause_probability"]:
                jitter = self._rng.randint(-60, 140)
                delivery.pause_after_ms = min(700, max(80, base_pause_ms + jitter))
        return delivery

    # -- self-correction ----------------------------------------------------

    def maybe_self_correct(self, text: str, *, language: str,
                           identity: VoiceIdentity | None = None) -> str:
        """Very rare, controlled restart ("Aapka payment... sorry, ...").

        Never applied to critical content; disabled by default.  Returns the
        text unchanged when no correction applies.
        """
        cfg = self._config
        if not (self.enabled and cfg["self_correction"]):
            return text
        if contains_critical_content(text):
            return text
        lang = base_language(language)
        tokens = _POOLS.get(lang, {}).get("correction_token") or ()
        words = text.split()
        if not tokens or len(words) < 6:
            return text
        if self._rng.random() >= cfg["self_correction_probability"]:
            return text
        lead = " ".join(words[:2])
        restart = " ".join(words[1:])
        token = self._rng.choice(tokens)
        return f"{lead}... {token}, {restart}"

    # -- backchannels --------------------------------------------------------

    def plan_backchannel(self, *, language: str,
                         identity: VoiceIdentity | None = None,
                         now: float | None = None) -> str:
        """Return a backchannel token to play, or "" when none should play.

        The caller (conversation brain) is responsible for the *turn-state*
        gates: caller has been speaking continuously long enough, bot silent,
        no generation in flight.  This method owns probability, spacing and
        variant choice.  A non-empty return value starts the cooldown clock.
        """
        cfg = self._config
        if not self.backchannels_enabled:
            return ""
        if self._backchannels_played >= cfg["max_backchannels_per_call"]:
            return ""
        lang = base_language(language)
        if lang not in _POOLS:
            return ""
        moment = time.monotonic() if now is None else now
        if self._last_backchannel_monotonic is not None:
            gap_s = cfg["min_gap_between_backchannels_ms"] / 1000.0
            if moment - self._last_backchannel_monotonic < gap_s:
                return ""
        if self._rng.random() >= cfg["backchannel_probability"]:
            # A failed roll still consumes the opportunity window so the
            # monitor does not immediately re-roll every tick.
            self._last_backchannel_monotonic = moment
            return ""
        token = self._pick(lang, "backchannel", identity)
        if token:
            self._last_backchannel_monotonic = moment
            self._backchannels_played += 1
        return token

    @property
    def backchannels_played(self) -> int:
        return self._backchannels_played
