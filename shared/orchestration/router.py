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

from shared.orchestration import lang as _lang
from shared.orchestration import signals as _signals


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
    # Semantic signal of the utterance (see classify_user_signal) — attached
    # to workflow decisions so the workflow layer can check whether the
    # current node actually supports what the caller just said.
    signal: str | None = None


# ── user-signal classification ──────────────────────────────────────────────
# Context-free semantic classification of a caller utterance (or of a
# workflow edge-label token) into the conversation signals the workflow layer
# reasons about. The MEANINGS live in signal packs
# (shared/orchestration/signals: core + domain packs), the SURFACE FORMS per
# language in language packs (shared/orchestration/lang). This module only
# composes them — it spells no caller-language word itself.

def classify_user_signal(text: str, *, languages: list[str] | None = None) -> str | None:
    """Semantic signal of an utterance (hardship, refusal, complaint, clarify,
    hold, callback, payment_intent, already_paid, wrong_person, agent_request,
    question, affirm — or None). Deliberately conservative (None over a guess).
    ``languages`` restricts the surface forms to a bot's languages; the
    default is every registered language."""
    return _signals.classify(text, languages=languages)


# Compatibility view of the classifier table for code that iterates it.
_SIGNAL_PATTERNS: list[tuple[str, re.Pattern]] = list(_signals.compiled())


# ── opening affirmation ──────────────────────────────────────────────────────
# A caller confirming the bot's opening question rarely says a bare "yes":
# "Yes, I am speaking", "हाँ हाँ, मैं बोल रहा हूँ", "haan ji boliye". The bare
# `affirm` signal above deliberately rejects those (a trailing clause may
# carry a different meaning), and sample matching scores a lone "yes" inside a
# five-word sentence far below any intent threshold — so the confirmation fell
# to plain chat and the configured workflow never started.
# Word ends are enforced with a not-another-letter lookahead over every
# registered script (Devanagari / Malayalam / Tamil vowel signs are combining
# marks outside ``\w``, so ``\b`` never forms after them).
_LEADING_AFFIRM = re.compile(
    r"^\W*(?:" + _lang.alternatives("leading_affirm_tokens") + r")"
    r"(?![\w" + _lang.letters() + r"])",
    re.I,
)
_AFFIRM_CONTRADICTION = re.compile(_lang.alternatives("contradiction"), re.I)
_LEADING_AFFIRM_MAX_TOKENS = 10


def leading_affirmation(text: str) -> bool:
    """Whether a short utterance OPENS with a confirmation and carries no
    contrary meaning ("Yes, I am speaking" → True; "yes but I want an agent",
    "haan nahi", a question, or a long explanation → False)."""
    stripped = (text or "").strip()
    if not stripped or not _LEADING_AFFIRM.search(stripped):
        return False
    if len(match_tokens(stripped)) > _LEADING_AFFIRM_MAX_TOKENS:
        return False
    if _AFFIRM_CONTRADICTION.search(stripped):
        return False
    return classify_user_signal(stripped) in (None, "affirm")


_SMALLTALK = re.compile(
    "|".join(p.smalltalk for p in _lang.packs() if p.smalltalk) or r"(?!)", re.IGNORECASE,
)

# ── multilingual call control (hang-up / do-not-call / emergency / consent) ─
# Deterministic, transcription-tolerant and checked before EVERYTHING else
# (including an active workflow): a caller asking to hang up, revoking
# contact consent or reporting an emergency must never receive another pitch,
# rung, clarification or LLM fallback. Each language pack contributes its own
# patterns; a negation ("don't hang up", "फोन मत काटो") from any language wins.
_HANGUP_NEGATION = _lang.compile_alternation("hangup_negation")
_HANGUP_PATTERNS: list[re.Pattern] = _lang.compile_any("hangup_patterns")


def detect_hangup(text: str) -> bool:
    """Deterministic multilingual hang-up intent."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if _HANGUP_NEGATION is not None and _HANGUP_NEGATION.search(stripped):
        return False
    return any(p.search(stripped) for p in _HANGUP_PATTERNS)


_DNC_PATTERNS: list[re.Pattern] = _lang.compile_any("dnc_patterns")


def detect_do_not_call(text: str) -> bool:
    """Deterministic 'never call me again' consent revocation."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    return any(p.search(stripped) for p in _DNC_PATTERNS)


_EMERGENCY = _lang.compile_alternation("emergency")


def detect_emergency(text: str) -> bool:
    """Emergency / safety language — escalate to a human, never a pitch."""
    stripped = (text or "").strip()
    return bool(stripped) and _EMERGENCY is not None and bool(_EMERGENCY.search(stripped))


# "don't record", "recording band karo" — consent refusal for recording.
_CONSENT_REFUSAL = _lang.compile_alternation("consent_refusal")


def detect_consent_refusal(text: str) -> bool:
    stripped = (text or "").strip()
    return bool(stripped) and _CONSENT_REFUSAL is not None and bool(_CONSENT_REFUSAL.search(stripped))


_CALL_CONTROL: list[tuple[re.Pattern, str]] = [
    # hang-up lives in detect_hangup() (multilingual + negation-guarded),
    # checked before this list ever runs.
    (re.compile(pattern, re.I), action)
    for pack in _lang.packs() for pattern, action in pack.call_control_patterns
]

_HANDOFF = re.compile(
    r"\b(" + "|".join(p.handoff_words for p in _lang.packs() if p.handoff_words) + r")\b", re.I,
)

# Question shapes that usually need tenant knowledge: the languages' question
# words plus the domain vocabulary of the active signal packs (generic
# service words in core, policy words in the insurance pack, …).
_KB_SIGNALS = re.compile(
    r"\b(" + "|".join([w for p in _lang.packs() for w in p.kb_question_words]
                      + list(_signals.knowledge_terms())) + r")\b",
    re.I,
)

# Platform safety: a caller reading out a secret. Not a language matter.
_UNSAFE = re.compile(
    r"\b(card number|cvv|otp|one[- ]time password|password) (is|was)?\s*[:\-]?\s*\d",
    re.I,
)


# ── knowledge questions inside other utterances ─────────────────────────────
# Clause boundaries: sentence punctuation and the connectors callers use to
# append a question to an answer ("… waise …", "… aur …", "… but …").
_CLAUSE_SPLIT = re.compile(
    r"[" + re.escape(_lang.sentence_terminators()) + r"]+|(?<!\w)(?:"
    + _lang.alternatives("clause_connectors") + r")(?!\w)",
    re.I,
)
# Question shape in every registered language (a "?" counts too). This wide
# form — interrogatives AND English auxiliaries anywhere — feeds knowledge
# vocabulary filtering; ``looks_like_question`` below is the stricter shape.
_QUESTION_MARKERS = re.compile(
    r"\?|(?<!\w)(?:" + _lang.alternatives("question_markers")
    + "|" + _lang.alternatives("question_aux") + r")(?!\w)",
    re.I,
)
_QUESTION_MARKERS_ANYWHERE = re.compile(
    r"\?|(?<!\w)(?:" + _lang.alternatives("question_markers") + r")(?!\w)", re.I,
)
# English auxiliaries open a question only at the start of a clause AND
# followed by an English subject ("is it refunded?", "do you support Tally?").
# Elsewhere they are Hinglish words: "is" = यह ("is baar"), "do" = दो ("bata
# do", "mark do hai" — an STT slip for "ho"), "are" = अरे ("Are maine deliver
# kar diya", cv_7786bc42deca), "can"/"will" inside a statement.
_QUESTION_CLAUSE_START = re.compile(
    r"^\W*(?:" + _lang.alternatives("question_aux") + r")\s+(?:"
    + _lang.alternatives("question_subjects") + r")(?!\w)", re.I,
)


def looks_like_question(text: str) -> bool:
    """Deterministic question shape: a "?" or an interrogative word in any
    registered language. The workflow engine uses it to double-check an LLM
    'question' label before that label is allowed to park a caller's literal
    answer off-script ("मैं टैली यूज़ करता हूँ।" was labelled a question with
    confidence 0.0 in live calls and re-asked six times).

    Wh-words count anywhere; an English auxiliary counts only when it opens a
    clause and an English subject follows ("do you", "is it"), so Hinglish
    statements containing "is"/"do"/"are" stay statements.
    """
    text = text or ""
    if _QUESTION_MARKERS_ANYWHERE.search(text):
        return True
    return any(_QUESTION_CLAUSE_START.match(clause) for clause in _CLAUSE_SPLIT.split(text))


# Function words that never identify a knowledge TOPIC (per language pack).
_KNOWLEDGE_STOP_TOKENS = _lang.union("stop_tokens")


# ── intent sample matching ───────────────────────────────────────────────────
# Samples match WHOLE WORDS, never mid-word substrings ("yes" must not fire
# inside "yesterday", "हाँ" not inside "कहाँ"). Tokenization is deliberately
# NOT \w+: Python's \w splits Indic words at every matra ("कहाँ" → "कह"), so
# tokens are whitespace-delimited words with surrounding punctuation stripped
# and hyphen/slash compounds split ("check-in" ≡ "check in").

_TOKEN_SEPARATORS = re.compile(r"[-–—/_]+")
_TOKEN_STRIP = _lang.token_strip_chars()


def match_tokens(text: str) -> list[str]:
    """Unicode-safe whole-word tokens for sample/utterance matching."""
    normalized = _TOKEN_SEPARATORS.sub(" ", (text or "").lower())
    return [t for t in (tok.strip(_TOKEN_STRIP) for tok in normalized.split()) if t]


def sample_match_strength(sample: str, utterance_tokens: list[str]) -> float:
    """How strongly an utterance expresses one configured sample phrase.

    Returns 0.0 for no match; otherwise a confidence that reflects the
    QUALITY of the evidence (not, as the old voting did, what fraction of the
    intent's samples happened to hit — a metric that got weaker every time an
    author added coverage):

    - the utterance IS the sample                       → 0.95
    - the sample phrase is contained contiguously       → 0.55 + 0.35 × coverage
    - all sample words appear in order with gaps
      ("confirm my *upcoming* booking"; ≥3-word samples) → 0.45 + 0.35 × coverage
    - a single-word sample appears as a whole word      → 0.25 + 0.35 × coverage

    where coverage = sample words / utterance words: a phrase that dominates
    the utterance scores near the top of its band, the same phrase buried in
    a long sentence scores near the bottom. This is what makes per-intent
    ``confidence_threshold`` values meaningful — a lone common word can never
    reach a high-threshold (handoff/destructive) intent, while an utterance
    that is essentially the configured phrase always can.
    """
    sample_tokens = match_tokens(sample)
    if not sample_tokens or not utterance_tokens:
        return 0.0
    m, n = len(sample_tokens), len(utterance_tokens)
    coverage = min(1.0, m / n)
    contiguous = m <= n and any(
        utterance_tokens[i:i + m] == sample_tokens for i in range(n - m + 1)
    )
    if contiguous:
        if m == n:
            return 0.95
        if m == 1:
            return 0.25 + 0.35 * coverage
        return 0.55 + 0.35 * coverage
    if m >= 3:
        position = 0
        for token in utterance_tokens:
            if token == sample_tokens[position]:
                position += 1
                if position == m:
                    return 0.45 + 0.35 * coverage
    return 0.0


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
        # Topic vocabulary of the tenant's KNOWLEDGE intents (their sample
        # phrases, minus function/question words): a clause that asks a
        # question AND names one of these topics is a knowledge question even
        # when it sits inside a workflow answer ("nahi bataya tha, waise
        # onboarding fee hoti kya hai?") — see detect_knowledge_question.
        vocab: set[str] = set()
        for intent in self._intents:
            if str(intent.get("route") or "") != "knowledge":
                continue
            for sample in intent.get("samples") or []:
                for token in match_tokens(str(sample)):
                    # Indic words are short in code points ("फी" = STT's
                    # spelling of fee is two), so the length floor is script-aware
                    # (each language pack declares its own floor).
                    floor = _lang.short_token_floor(token)
                    if len(token) >= floor and token not in _KNOWLEDGE_STOP_TOKENS \
                            and not _QUESTION_MARKERS.fullmatch(token):
                        vocab.add(token)
        self._knowledge_vocab = vocab
        self._workflows = workflows or {}
        self._affirm_entry = self._derive_affirm_entry()

    @property
    def affirm_entry(self) -> tuple[str, str] | None:
        """(intent name, workflow action) a plain confirmation of the bot's
        opening question routes to, or None. Derived ONLY from tenant config:
        an intent that lists a bare confirmation ("haan", "yes", "theek hai")
        among its samples is, by the author's own declaration, the opening
        confirmation intent. Nothing bot- or tenant-specific lives here."""
        return self._affirm_entry

    def apply_entry_fallback(
        self,
        decision: RouteDecision,
        *,
        active_workflow: str | None = None,
        allow_affirm_entry: bool = True,
    ) -> RouteDecision:
        """Keep an unclear opening reply on its configured pending question.

        Run after semantic intent routing: a meaningful workflow, knowledge,
        handoff or call-control decision must win. Only an opening intent that
        explicitly declares ``fallback_behavior=clarify`` opts in. The caller
        of this method speaks the pending opening question with a short
        clarification, without invoking the free-chat system prompt.
        """
        if (
            active_workflow is not None
            or not allow_affirm_entry
            or self._affirm_entry is None
            or decision.kind not in (RouteKind.CHAT, RouteKind.CLARIFY)
            or decision.signal not in (None, "clarify")
        ):
            return decision
        entry_name, _ = self._affirm_entry
        configured = next(
            (intent for intent in self._intents if intent.get("name") == entry_name),
            None,
        )
        if not configured or configured.get("fallback_behavior") != "clarify":
            return decision
        return RouteDecision(
            kind=RouteKind.CLARIFY,
            confidence=decision.confidence,
            reason="entry_reprompt",
            action="repeat_entry_question",
            intent=entry_name,
            considered_kb=decision.considered_kb,
            signal=decision.signal,
        )

    def _derive_affirm_entry(self) -> tuple[str, str] | None:
        candidates: dict[str, str] = {}
        for intent in self._intents:
            route = str(intent.get("route") or "")
            if route.startswith("workflow:"):
                action = route.split(":", 1)[1]
            elif intent.get("workflow_id"):
                action = str(intent["workflow_id"])
            else:
                continue
            if not action:
                continue
            samples = intent.get("samples") or []
            if any(classify_user_signal(str(s)) == "affirm" for s in samples):
                candidates.setdefault(action, str(intent.get("name") or ""))
        if len(candidates) != 1:
            # No opening-confirmation intent, or two of them pointing at
            # DIFFERENT workflows — a guess here would start the wrong flow.
            return None
        action, name = next(iter(candidates.items()))
        return name, action

    def decide(
        self,
        text: str,
        *,
        active_workflow: str | None = None,
        allow_affirm_entry: bool = True,
    ) -> RouteDecision:
        stripped = (text or "").strip()
        if not stripped:
            return RouteDecision(kind=RouteKind.CLARIFY, confidence=0.3, reason="empty_input")

        # 0a. Hang-up outranks everything, including an active workflow: a
        # caller asking to end the call must never get another pitch, rung,
        # clarification or LLM fallback (any language).
        if detect_hangup(stripped):
            return RouteDecision(kind=RouteKind.CALL_CONTROL, action="hangup",
                                 reason="hangup_phrase")

        # 0b. Consent revocation ("never call me again") and emergencies are
        # platform-critical: deterministic, ahead of workflows and the LLM.
        if detect_do_not_call(stripped):
            return RouteDecision(kind=RouteKind.CALL_CONTROL, action="do_not_call",
                                 reason="dnc_phrase")
        if detect_emergency(stripped):
            return RouteDecision(kind=RouteKind.HANDOFF, action="transfer",
                                 reason="emergency")

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
                kind=RouteKind.WORKFLOW, reason=f"active_workflow:{active_workflow}",
                signal=classify_user_signal(stripped),
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

        # 3. Configured intents (whole-word sample matching, per-intent
        # confidence thresholds). This must precede the
        # generic smalltalk shortcut: when a bot explicitly configures "yes"
        # or "haan" as its opening confirmation, that answer must start the
        # workflow instead of being sent to the LLM as casual smalltalk.
        intent = self._match_intent(stripped)
        if intent is not None:
            name, route, confidence = intent
            if route and route.startswith("workflow:"):
                return RouteDecision(kind=RouteKind.WORKFLOW, intent=name, confidence=confidence,
                                     action=route.split(":", 1)[1], reason="intent_workflow",
                                     signal=classify_user_signal(stripped))
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

        # A caller may answer an identifier request with ONLY the value. If
        # the semantic decision model is slow/unavailable, a bare/spoken ID
        # has no sample phrase to match and used to fall into plain chat — the
        # LLM then guessed whether it was valid instead of running the saved
        # verification workflow. Identifier-bearing workflow intents provide
        # enough tenant-authored evidence to route deterministically, but only
        # when every matching intent points to the SAME workflow.
        identifier_intent = self._match_identifier_workflow(stripped)
        if identifier_intent is not None:
            name, action = identifier_intent
            return RouteDecision(
                kind=RouteKind.WORKFLOW, intent=name, confidence=0.95,
                action=action, reason="identifier_workflow",
                signal=classify_user_signal(stripped),
            )

        # 3b. Confirmation of the bot's opening question. The tenant declared
        # which workflow a plain "yes/haan" starts (see affirm_entry); natural
        # confirmations ("Yes, I am speaking", "हाँ हाँ, मैं बोल रहा हूँ")
        # score too low for sample matching yet mean exactly that. The brain
        # withdraws permission once any workflow has run on the call, so a
        # closing "theek hai" can never restart the flow.
        if allow_affirm_entry and self._affirm_entry and leading_affirmation(stripped):
            name, action = self._affirm_entry
            return RouteDecision(
                kind=RouteKind.WORKFLOW, intent=name, confidence=0.6, action=action,
                reason="affirm_entry_workflow", signal="affirm",
            )

        # 4. Unconfigured smalltalk never hits the knowledge base.
        if _SMALLTALK.match(stripped):
            return RouteDecision(kind=RouteKind.CHAT, reason="smalltalk", considered_kb=True)

        # 5. Knowledge decision — question-shaped + domain terms + KBs exist.
        if self._has_kbs:
            kb_hit = bool(_KB_SIGNALS.search(stripped)) or bool(
                self._kb_extra and self._kb_extra.search(stripped)
            )
            wordish = len(stripped.split()) >= 3
            if kb_hit and wordish:
                return RouteDecision(kind=RouteKind.KNOWLEDGE, confidence=0.8,
                                     reason="kb_signals", considered_kb=True)

        # 6. Very short input: only truly AMBIGUOUS shorts earn a canned
        # clarification. A short utterance that carries a semantic signal
        # ("haan", "नहीं", "busy", "ओके") is a meaningful reply to whatever
        # the bot just asked — the LLM answers it in context.
        if len(stripped.split()) <= 2:
            signal = classify_user_signal(stripped)
            if signal is None:
                return RouteDecision(kind=RouteKind.CLARIFY, confidence=0.4,
                                     reason="too_short", considered_kb=True)
            return RouteDecision(kind=RouteKind.CHAT, confidence=0.5,
                                 reason="short_signal", signal=signal)

        return RouteDecision(kind=RouteKind.CHAT, confidence=0.6, reason="default_chat",
                             considered_kb=self._has_kbs)

    def detect_knowledge_question(self, text: str) -> str | None:
        """The clause of ``text`` that asks a tenant-knowledge question, or None.

        Callers regularly fold a question into an answer ("300 bola tha but
        400 cut gaya. Waise har store ki fee same hoti hai kya?"). The
        utterance is split into clauses; a clause is a knowledge question when
        a knowledge-route intent sample matches it, or when it is shaped like a
        question AND names a topic from the knowledge intents' vocabulary.
        Returns that clause (the retrieval query). Bots without knowledge
        bases never return a clause.
        """
        if not self._has_kbs:
            return None
        stripped = (text or "").strip()
        if not stripped:
            return None
        clauses = [c.strip() for c in _CLAUSE_SPLIT.split(stripped) if c and c.strip()]
        if not clauses:
            clauses = [stripped]
        for clause in clauses:
            matched = self._match_intent(clause)
            if matched is not None and str(matched[1] or "") == "knowledge":
                return clause
        if self._knowledge_vocab:
            for clause in clauses:
                if not _QUESTION_MARKERS.search(clause):
                    continue
                # At least TWO distinct topic words: one shared word ("store"
                # in "store manager ka number kya hai?") is not a knowledge
                # question — that turn belongs to the ordinary off-script path.
                hits = {t for t in match_tokens(clause) if t in self._knowledge_vocab}
                if len(hits) >= 2:
                    return clause
        return None

    def _match_intent(self, text: str) -> tuple[str, str | None, float] | None:
        """Best configured intent for the utterance, as (name, route, confidence).

        Confidence is the strongest single sample match (see
        :func:`sample_match_strength`) and is gated by the intent's own
        ``confidence_threshold`` — an author expresses the intent's RISK
        there: destructive or handoff intents demand strong phrase-level
        evidence, informational ones may accept a weaker match. An utterance
        that IS a configured sample (0.95) always counts: the caller said the
        exact configured phrase.
        """
        utterance_tokens = match_tokens(text)
        best: tuple[str, str | None, float] | None = None
        for intent in self._intents:
            samples = [s for s in (intent.get("samples") or []) if s]
            if not samples:
                continue
            strength = max(
                (sample_match_strength(s, utterance_tokens) for s in samples),
                default=0.0,
            )
            if strength <= 0.0:
                continue
            threshold = float(intent.get("confidence_threshold") or 0.5)
            if strength < threshold and strength < 0.95:
                continue
            if best is None or strength > best[2]:
                best = (intent.get("name", "intent"), intent.get("route"), strength)
        return best

    def _match_identifier_workflow(self, text: str) -> tuple[str, str] | None:
        """Unique configured workflow for a caller-dictated identifier.

        This is deliberately schema-light: intent entity NAMES are existing
        author-controlled configuration. Values such as amounts/dates never
        trigger it; only conventional identifier names do, and ambiguous
        routes fail closed to the normal router path.
        """
        from shared.orchestration.spoken_numbers import (
            digits_dominant,
            spoken_digit_sequence,
        )

        if not digits_dominant(text):
            return None
        digits = spoken_digit_sequence(text)
        if not 4 <= len(digits) <= 18:
            return None

        identifier_name = re.compile(
            r"(?:^|_)(?:.*_?id|phone|mobile|reference|ref|account|policy|"
            r"claim|number)(?:$|_)",
            re.I,
        )
        candidates: list[tuple[str, str]] = []
        for intent in self._intents:
            route = str(intent.get("route") or "")
            workflow_id = str(intent.get("workflow_id") or "")
            action = route.split(":", 1)[1] if route.startswith("workflow:") else workflow_id
            if not action:
                continue
            entities = (
                list(intent.get("entities") or [])
                + list(intent.get("optional_entities") or [])
            )
            if any(identifier_name.search(str(entity)) for entity in entities):
                candidates.append((str(intent.get("name") or "intent"), action))
        actions = {action for _, action in candidates}
        if len(actions) != 1:
            return None
        action = next(iter(actions))
        name = next(name for name, candidate_action in candidates
                    if candidate_action == action)
        return name, action
