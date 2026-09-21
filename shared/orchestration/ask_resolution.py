"""Ask-node answer resolution — a configurable pipeline, not a special-case ladder.

An ``ask`` node collects one variable from the caller's utterance. Resolving
that utterance used to be a 280-line branch inside the interpreter step that
grew a new ``elif`` per live incident. It is now a pipeline of small, named
stages with one shared :class:`AskContext`:

1. **pre-handlers** — turns the ask answers itself without resolving a value
   (identifier restart, "what did you note?" readback); a hit ends the turn.
2. **value resolvers** — each may set ``ctx.value`` (and flags); the first
   resolver that *claims* the turn stops the chain (semantic-slots provider,
   narrative guard, standard matcher ladder).
3. **outcome stages** — ordered rules that turn the resolved state into a
   decision (slot filled → advance; joint partial → advance; digit overflow /
   partial → hold; semantic re-ask; signal without an edge → off-script or
   authored reply; captured a later field → off-script; else retry ladder).

The default stage order reproduces the historical behaviour exactly (frozen
by the golden replays). A node may restrict or reorder the VALUE resolvers
with ``askResolvers: ["semantic_slots", "standard"]``; extensions register
additional resolvers by name with :func:`register_resolver`. Behaviour
knobs that change outcomes live in :mod:`shared.orchestration.behavior`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from shared.orchestration import lang as _lang
from shared.orchestration import signals as _signals
from shared.orchestration.behavior import WorkflowBehavior
from shared.orchestration.phrases import canned
from shared.orchestration.router import looks_like_question


def _node_config(node: dict) -> dict:
    config = node.get("config")
    return config if isinstance(config, dict) else {}


def _ask_entity(node: dict, variable: str) -> dict:
    """Entity descriptor for an ask node, feeding the shared entity extractor."""
    config = _node_config(node)
    entity = config.get("entity")
    if isinstance(entity, dict) and entity:
        return {"name": variable, **entity}
    return {
        "name": variable,
        "dataType": str(config.get("entityType") or config.get("dataType") or "text"),
        "regexPattern": config.get("pattern"),
        "allowedValues": config.get("allowedValues"),
        "synonyms": config.get("synonyms"),
    }


def _ask_is_free_text(node: dict, variable: str) -> bool:
    """A free-text ask accepts ANY utterance as its answer — it needs the
    off-script guard so a complaint or question is not swallowed as a slot."""
    entity = _ask_entity(node, variable)
    has_matcher = bool(
        entity.get("regexPattern") or entity.get("regexPatterns")
        or entity.get("allowedValues") or entity.get("synonyms")
        or entity.get("synonymPatterns")
    )
    return str(entity.get("dataType") or "text") == "text" and not has_matcher


def _ask_expects_digits(node: dict, variable: str) -> bool:
    """Whether this ask collects a numeric identifier (booking ID, OTP, …)."""
    from shared.orchestration.entity_extractor import _expects_digits

    return _expects_digits(_ask_entity(node, variable))


# A dictated identifier can be held across turns while the caller pauses;
# anything longer than this is no longer an identifier being dictated.
_MAX_PENDING_DIGITS = 32

# Explicit identifier restart — the buffered partial is discarded and the
# caller re-dictates ("start again", "dobara", "phir se", "that was wrong",
# "clear the number"). Only consulted at ask nodes that expect digits.
# Surface forms per language pack.
_DIGITS_RESTART_RE = _lang.compile_alternation("digits_restart") or re.compile(r"(?!)")

# "What did you note so far?" — answered from the actual pending buffer,
# never with a claim that nothing was heard when digits are held.
_DIGITS_READBACK_RE = _lang.compile_alternation("digits_readback") or re.compile(r"(?!)")


def _digits_readback_reply(node: dict, variable: str, digits: str, lang: str) -> str:
    """Speakable summary of the pending identifier buffer.

    Sensitive identifiers (phone numbers, masked/PII entities) are never read
    back in full — only the digit count and the last two digits.
    """
    if not digits:
        return canned("wf_digits_none", lang)
    entity = _ask_entity(node, variable)
    sensitive = (
        bool(entity.get("pii"))
        or bool(entity.get("maskingEnabled") or entity.get("masking_enabled"))
        or str(entity.get("dataType") or entity.get("data_type") or "") == "phone"
    )
    if sensitive:
        key, spoken = "wf_digits_readback_masked", " ".join(digits[-2:])
    else:
        key, spoken = "wf_digits_readback", " ".join(digits)
    return (
        canned(key, lang)
        .replace("{count}", str(len(digits)))
        .replace("{digits}", spoken)
    )


def _apply_also_capture(node: dict, text: str, slots: dict,
                        audit: list, node_id: str) -> None:
    """Opt-in multi-answer capture for ask nodes (config ``alsoCapture``).

    One caller utterance often answers several upcoming questions at once
    ("haan call kiya tha aur guard ko de diya"). An ask node may declare
    ``alsoCapture: [{"variable": ..., "entity": {...}}, ...]`` — after the
    node's OWN slot is filled from an utterance, each listed entity matcher
    runs against the same utterance and fills its slot when it matches, so
    the later ask for that slot is skipped (slot_reused) instead of
    mechanically re-asking what the caller already said. Matcher-based and
    empty-slot-only by default: a free-text guess can never overwrite or
    invent an answer. A correction node may explicitly set ``overwrite:
    true`` for a capture, implementing "latest clear answer wins" without
    weakening ordinary multi-answer collection.

    A spec may instead set ``clear: true``: when its matcher fires the slot is
    REMOVED ("the call part is wrong" — a field named without its new value),
    so the flow's next walk re-asks exactly that question and nothing else.
    Clears run before captures, so "call wala galat hai, maine call nahi kiya
    tha" clears and then re-fills the same slot in one utterance.
    """
    from shared.orchestration.entity_extractor import extract_entity

    specs = [
        spec for spec in (_node_config(node).get("alsoCapture") or [])
        if isinstance(spec, dict)
    ]
    ordered = ([s for s in specs if s.get("clear") is True]
               + [s for s in specs if s.get("clear") is not True])
    for spec in ordered:
        variable = str(spec.get("variable") or "").strip()
        entity = spec.get("entity")
        if not variable or not isinstance(entity, dict) or not entity:
            continue
        previous = str(slots.get(variable) or "").strip()
        if spec.get("clear") is True:
            if not previous:
                continue  # nothing to clear
            extracted = extract_entity(text, {"name": variable, **entity})
            if extracted.get("matched"):
                slots.pop(variable, None)
                for dependent in spec.get("invalidateSlots") or []:
                    slots.pop(dependent, None)
                audit.append({"action": "also_cleared", "node": node_id,
                              "variable": variable})
            continue
        overwrite = spec.get("overwrite") is True
        if previous and not overwrite:
            continue  # never overwrite an answer the caller already gave
        extracted = extract_entity(text, {"name": variable, **entity})
        if not extracted.get("matched"):
            continue
        value = str(extracted.get("value")
                    or extracted.get("maskedValue") or "").strip()
        if value:
            if value != previous:
                for dependent in spec.get("invalidateSlots") or []:
                    if dependent in slots:
                        audit.append({"action": "also_invalidated", "node": node_id,
                                      "variable": dependent})
                    slots.pop(dependent, None)
            slots[variable] = value
            audit.append({"action": ("also_updated" if previous else
                                      "also_captured"), "node": node_id,
                          "variable": variable})


def _captures_other_field(node: dict, text: str, slots: dict, audit: list,
                          node_id: str, *, already: bool) -> bool:
    """Run the ask's alsoCapture set (once per turn) and report whether any
    downstream slot changed — the utterance was an answer, just not to THIS ask."""
    if already:
        return False
    before = len(audit)
    _apply_also_capture(node, text, slots, audit, node_id)
    return any(entry.get("action") in _HUB_CAPTURE_ACTIONS for entry in audit[before:])


# Audit actions that mean "this turn's utterance already changed an earlier
# answer" — a correction ask marked ``skipIfCorrectedThisTurn`` steps aside
# when one of these was recorded before the walk reached it.
_CORRECTION_ACTIONS = frozenset({"also_updated", "also_cleared"})
# At an intent hub, ANY capture from the utterance (a new downstream fact,
# an overwrite or a clear) shows the caller answered this hub — "haan, uska
# naam Raju hai" answered "did you ask the name?" whatever the LLM labelled it.
_HUB_CAPTURE_ACTIONS = frozenset({"also_captured", "also_updated", "also_cleared"})


_YES_NO_SIGNALS = _signals.names_where("yes_no")
# Bare surfaces that identify a canonical as the YES / NO answer of a yes-no
# ask. Whole strings (never substrings): a lexicon surface such as "हा" or
# "ji" would also match inside "रहा" / "jiska", which is exactly why STT
# variants of "हाँ" are resolved from the SIGNAL instead of the lexicon.
# (Surface forms come from the language packs.)
_BARE_YES = _lang.union("canonical_yes")
_BARE_NO = _lang.union("canonical_no")


def _yes_no_canonicals(entity: dict) -> tuple[str, str] | None:
    """(yes_canonical, no_canonical) of a yes-no shaped ask entity, else None.

    Shape = exactly one canonical whose surfaces include a bare yes word and
    exactly one whose surfaces include a bare no word. A recipient choice, a
    free-text ask or a numeric identifier never qualifies.
    """
    synonyms = entity.get("synonyms")
    if not isinstance(synonyms, dict):
        return None

    def _polarity(canonical, surfaces) -> str | None:
        # Platform canonicals read "yes (called the customer)" / "no (did not
        # call)": the leading word decides. A lookahead entity that carries
        # only explicit phrases (no bare "haan") is still yes-no shaped.
        first = re.split(r"[\s(]+", str(canonical).strip().lower(), maxsplit=1)[0]
        if first in _BARE_YES:
            return "yes"
        if first in _BARE_NO:
            return "no"
        if isinstance(surfaces, (list, tuple)):
            lowered = {str(x).strip().lower() for x in surfaces}
            if lowered & _BARE_YES:
                return "yes"
            if lowered & _BARE_NO:
                return "no"
        return None

    yes = [c for c, surfaces in synonyms.items() if _polarity(c, surfaces) == "yes"]
    no = [c for c, surfaces in synonyms.items() if _polarity(c, surfaces) == "no"]
    if len(yes) == 1 and len(no) == 1 and yes[0] != no[0]:
        return str(yes[0]), str(no[0])
    return None


# A "bare" answer is only affirmation/negation (plus fillers): no field-level
# evidence at all. Used for asks that put TWO yes-no questions in one breath
# ("location par pahunche the aur call kiya tha?"): a bare "हाँ" answers both,
# while any explicit content is attributed field by field.
_BARE_YES_WORDS = _lang.union("bare_yes")
_BARE_NO_WORDS = _lang.union("bare_no")
_BARE_FILLERS = _lang.union("fillers")
_BARE_TOKEN_SPLIT = re.compile(
    r"[\s,;:\-\"'()\[\]" + re.escape(_lang.sentence_terminators()) + r"]+"
)


def _bare_yes_no(text: str) -> str | None:
    """"yes" / "no" when the utterance is ONLY affirmation/negation words."""
    tokens = [t.lower() for t in _BARE_TOKEN_SPLIT.split(text or "") if t]
    if not tokens:
        return None
    yes = no = False
    for token in tokens:
        if token in _BARE_NO_WORDS:
            no = True
        elif token in _BARE_YES_WORDS:
            yes = True
        elif token in _BARE_FILLERS:
            continue
        else:
            return None
    if no:
        return "no"      # "ji nahi" / "haan nahi" — the negation is the answer
    return "yes" if yes else None


def _strip_bare_words(text: str) -> str:
    """The utterance without its leading/trailing yes-no and filler words, so
    "हाँ, customer को call किया था" leaves only the field-level evidence."""
    # Whitespace tokens, compared with their punctuation stripped, so
    # "didn't" / "customer's" keep their apostrophes for the field matchers.
    tokens = (text or "").split()
    # Tense/auxiliary fillers (tha/था/hai) stay: the field matchers may rely
    # on them ("गया था"); only the yes-no words and discourse fillers go.
    skip = _BARE_YES_WORDS | _BARE_NO_WORDS | _lang.union("strip_fillers")
    punct = ",;:-\"'()[]" + _lang.sentence_terminators()

    def _bare(token: str) -> bool:
        return token.strip(punct).lower() in skip

    while tokens and _bare(tokens[0]):
        tokens.pop(0)
    while tokens and _bare(tokens[-1]):
        tokens.pop()
    return " ".join(tokens).strip(" ,;:" + _lang.sentence_terminators())


def _without_bare_surfaces(entity: dict) -> dict:
    """The entity minus its bare yes/no surfaces ("haan", "nahi", "नहीं"…).

    At a joint yes-no ask a non-bare utterance is judged on field evidence
    only: an inner "नहीं" in "मैंने customer को call नहीं किया था" belongs to the
    call, never to the location slot."""
    synonyms = entity.get("synonyms")
    if not isinstance(synonyms, dict):
        return entity
    bare = _BARE_YES_WORDS | _BARE_NO_WORDS
    cleaned = {
        canonical: [x for x in (surfaces or []) if str(x).strip().lower() not in bare]
        for canonical, surfaces in synonyms.items()
        if isinstance(surfaces, (list, tuple))
    }
    return {**entity, "synonyms": cleaned}


def _joint_yes_no_variables(config: dict) -> list[str]:
    joint = config.get("jointYesNo")
    if not isinstance(joint, list):
        return []
    return [str(item).strip() for item in joint if str(item or "").strip()]


def _joint_entity(config: dict, variable: str) -> dict | None:
    """The alsoCapture entity that describes a joint yes-no variable."""
    for spec in config.get("alsoCapture") or []:
        if isinstance(spec, dict) and str(spec.get("variable") or "") == variable:
            entity = spec.get("entity")
            if isinstance(entity, dict):
                return entity
    return None


def _yes_no_from_signal(node: dict, variable: str, signal: str | None) -> str | None:
    """A bare affirmation/refusal IS the answer to a yes-no ask.

    cv_5729e30fad60: the partner answered the reached+called question with
    "हा." (Gujarati STT transliteration of हाँ). The lexicon knew "हाँ" only,
    the turn went off-script (signal=affirm), the LLM improvised a guard
    question, and the partner's next "नहीं" landed in the still-pending ask as
    reached = no. The semantic signal already says yes/no — use it.
    """
    if signal not in _YES_NO_SIGNALS:
        return None
    pair = _yes_no_canonicals(_ask_entity(node, variable))
    if pair is None:
        return None
    return pair[0] if signal == "affirm" else pair[1]


def _extract_ask_value(node: dict, variable: str, text: str) -> str | None:
    from shared.orchestration.entity_extractor import extract_entity

    entity = _ask_entity(node, variable)
    data_type = str(entity.get("dataType") or "text")
    has_matcher = bool(
        entity.get("regexPattern") or entity.get("regexPatterns")
        or entity.get("allowedValues") or entity.get("synonyms")
        or entity.get("synonymPatterns")
    )
    if data_type == "text" and not has_matcher:
        # Free-text answer: take the utterance as-is.
        return text.strip() or None
    extracted = extract_entity(text, entity)
    if not extracted.get("matched"):
        return None
    return str(extracted.get("value") or extracted.get("maskedValue") or "").strip() or None


# ── the pipeline ─────────────────────────────────────────────────────────────

_OFF_SCRIPT_SIGNALS = _signals.names_where("off_script")
# Signals at a semantic-slots ask that the brain answers instead of the fixed
# unmatched reply (a question / hold / callback / agent request while the
# extractor is authoritative for the facts).
_SEMANTIC_DEFERRED_SIGNALS = frozenset({"question", "hold", "callback", "agent_request"})


@dataclass
class AskContext:
    """Everything one ask-node turn reads and writes. Mutable collections are
    the interpreter's own (slots/audit/… are mutated in place)."""

    node: dict
    node_id: str
    text: str
    signal: str | None
    lang: str
    slots: dict
    audit: list
    pending_digits: dict
    node_retries: dict
    replies: list
    behavior: WorkflowBehavior
    # engine callbacks (closures over the definition)
    ask_question: Callable[[dict, bool, str], str]
    unmatched_reply: Callable[[dict, str | None, str], str]
    next_of: Callable[[str], str | None]
    fallback_target: Callable[[str], str | None]
    # semantic-slots extension (optional)
    provider: Any = None
    semantic: dict | None = None
    # derived on construction
    config: dict = field(default_factory=dict)
    variable: str = ""
    semantic_active: bool = False
    semantic_answers: bool = False
    semantic_ask: bool = False
    narrative_ask: bool = False
    guarded: bool = False
    expects_digits: bool = False
    buffered: str = ""
    dictated: bool = False
    fresh: str = ""
    combined: str = ""
    max_digits: int = 0
    joint_vars: list = field(default_factory=list)
    # working state
    value: Any = None
    handled: bool = False
    accumulated: bool = False
    captured_first: bool = False
    joint_partial: bool = False
    # outcome
    current: str | None = None
    awaiting: str | None = None
    off_script: bool = False
    status: str | None = None

    def __post_init__(self) -> None:
        self.config = _node_config(self.node)
        self.variable = str(self.config.get("variable") or self.node.get("id"))
        self.semantic_active = isinstance(self.semantic, dict)
        self.semantic_answers = bool(self.semantic_active and self.semantic.get("patch"))
        self.semantic_ask = bool(self.semantic_active and self.provider is not None
                                 and self.provider.owns_variable(self.variable))
        self.narrative_ask = bool(self.semantic_active and self.provider is not None
                                  and self.provider.is_narrative_ask(self.node, self.variable))
        self.guarded = self.signal in _OFF_SCRIPT_SIGNALS or self.signal == "agent_request"
        self.awaiting = self.node_id  # default: the ask stays open

    def stay(self) -> None:
        self.current = None
        self.awaiting = self.node_id

    def advance(self) -> None:
        self.current, self.awaiting = self.next_of(self.node_id), None


Stage = Callable[[AskContext], Any]

# ── 1. pre-handlers ──────────────────────────────────────────────────────────

def reconcile_question_label(ctx: AskContext) -> None:
    """Behaviour v2: an LLM 'question' label yields to a statement of enough
    words at a free-text ask — the words ARE the answer (cv_7786bc42deca)."""
    if (
        ctx.guarded and ctx.signal == "question"
        and ctx.behavior.question_label_yields_free_text
        and not looks_like_question(ctx.text)
        and len(ctx.text.split()) >= ctx.behavior.literal_answer_min_words
        and _ask_is_free_text(ctx.node, ctx.variable)
    ):
        ctx.audit.append({"action": "question_label_yielded",
                          "node": ctx.node_id, "words": len(ctx.text.split())})
        ctx.guarded = False


def prepare_digits(ctx: AskContext) -> None:
    """Numeric-identifier dictation state: digits held from earlier turns of
    THIS ask continue the same identifier."""
    ctx.expects_digits = _ask_expects_digits(ctx.node, ctx.variable)
    ctx.buffered = ctx.pending_digits.get(ctx.node_id, "") if ctx.expects_digits else ""


def digits_restart(ctx: AskContext) -> bool:
    """Explicit restart: the buffered partial is wrong — drop it. When the
    same utterance re-dictates digits ("wrong — seven zero…"), they seed the
    fresh buffer below."""
    from shared.orchestration.spoken_numbers import spoken_digit_sequence

    if not (ctx.expects_digits and ctx.buffered and _DIGITS_RESTART_RE.search(ctx.text)):
        return False
    ctx.pending_digits.pop(ctx.node_id, None)
    ctx.buffered = ""
    ctx.audit.append({"action": "identifier_reset", "node": ctx.node_id, "reason": "caller_restart"})
    if not spoken_digit_sequence(ctx.text):
        ctx.replies.append(canned("wf_digits_restart", ctx.lang))
        ctx.stay()  # no retry burned
        return True
    return False


def digits_readback(ctx: AskContext) -> bool:
    """"What did you note?" — answer from the ACTUAL pending buffer (masked
    for sensitive fields), never a claim that nothing was heard."""
    if not (ctx.expects_digits and _DIGITS_READBACK_RE.search(ctx.text)):
        return False
    ctx.replies.append(_digits_readback_reply(ctx.node, ctx.variable, ctx.buffered, ctx.lang))
    ctx.audit.append({"action": "digits_readback", "node": ctx.node_id, "held_digits": len(ctx.buffered)})
    ctx.stay()  # no retry burned
    return True


def prepare_dictation(ctx: AskContext) -> None:
    from shared.orchestration.entity_extractor import identifier_length_bounds
    from shared.orchestration.spoken_numbers import digits_dominant, spoken_digit_sequence

    ctx.dictated = not ctx.handled and ctx.expects_digits and digits_dominant(ctx.text)
    ctx.fresh = spoken_digit_sequence(ctx.text) if ctx.dictated else ""
    ctx.combined = (ctx.buffered + ctx.fresh)[:_MAX_PENDING_DIGITS]
    ctx.max_digits = (identifier_length_bounds(_ask_entity(ctx.node, ctx.variable))[1]
                      if ctx.expects_digits else _MAX_PENDING_DIGITS)
    ctx.joint_vars = (_joint_yes_no_variables(ctx.config)
                      if not ctx.expects_digits and not ctx.semantic_ask else [])


# ── 2. value resolvers ───────────────────────────────────────────────────────
# Return True to CLAIM the turn (no later resolver runs), False/None to let the
# next resolver look.

def resolve_joint_yes_no(ctx: AskContext) -> bool:
    """ONE question, TWO yes-no fields (cv_f07c65c4cdb5: "location par
    pahunche the aur call kiya tha?" → "हाँ" must answer both). Priority:
    explicit field evidence (patterns / lexicon, via alsoCapture and the own
    matcher on the text WITHOUT its bare yes-no words) → a bare yes/no fills
    every still-open joint field → a partial explicit answer fills only its
    field and the node advances so the flow's single ask collects the other
    half. Never guess the other half. Enriches; never claims."""
    if ctx.handled or not ctx.joint_vars:
        return False
    node, config, text = ctx.node, ctx.config, ctx.text
    bare = _bare_yes_no(text)
    stripped = _strip_bare_words(text)
    before = len(ctx.audit)
    if bare is None and stripped:
        _apply_also_capture(node, stripped, ctx.slots, ctx.audit, ctx.node_id)
    ctx.captured_first = True
    joint_hit = any(
        entry.get("action") in _HUB_CAPTURE_ACTIONS and entry.get("variable") in ctx.joint_vars
        for entry in ctx.audit[before:]
    )
    if bare is not None:
        own_pair = _yes_no_canonicals(_ask_entity(node, ctx.variable))
        if own_pair is not None:
            ctx.value = own_pair[0] if bare == "yes" else own_pair[1]
        for joint_var in ctx.joint_vars:
            if ctx.slots.get(joint_var) not in (None, ""):
                continue
            entity = _joint_entity(config, joint_var)
            pair = _yes_no_canonicals(entity or {})
            if pair is not None:
                ctx.slots[joint_var] = pair[0] if bare == "yes" else pair[1]
                ctx.audit.append({"action": "joint_yes_no", "node": ctx.node_id,
                                  "variable": joint_var, "answer": bare})
    else:
        if stripped:
            evidence_node = {**node, "config": {
                **config, "entity": _without_bare_surfaces(_ask_entity(node, ctx.variable)),
            }}
            ctx.value = _extract_ask_value(evidence_node, ctx.variable, stripped)
        if ctx.value is None and joint_hit:
            ctx.joint_partial = True
    return False


def resolve_semantic_slots(ctx: AskContext) -> bool:
    """The semantic-slots provider has already resolved EVERY fact it owns.
    A regex or generic refusal signal must not attribute a different field's
    answer to the pending question."""
    if ctx.handled or not ctx.semantic_ask:
        return False
    ctx.value = ctx.slots.get(ctx.variable)
    ctx.captured_first = True
    if ctx.value is None and ctx.config.get("jointYesNo"):
        ctx.joint_partial = any(ctx.slots.get(key) for key in ctx.config["jointYesNo"])
    return True


def _understood_narrative(node: dict, semantic: dict | None, signal: str | None) -> bool:
    """Opt-in acceptance of an intelligible incident without inventing facts.

    Extraction can understand a deduction complaint even when none of the
    four delivery questions has been answered. Keep human-agent requests
    and failed extraction on their existing paths.
    """
    return bool(
        _node_config(node).get("acceptUnderstoodNarrative") is True
        and semantic and semantic.get("understood") is True
        and not semantic.get("failed") and signal != "agent_request"
        and (bool(semantic.get("patch")) or signal not in {"affirm", "refusal", "clarify"})
    )


def resolve_narrative_guard(ctx: AskContext) -> bool:
    """A garbled first response is not an incident narrative (semantic-slots
    definitions only)."""
    if ctx.handled or not ctx.narrative_ask:
        return False
    if _understood_narrative(ctx.node, ctx.semantic, ctx.signal):
        ctx.value = ctx.text.strip() or None
        return True
    if ctx.semantic_answers or (ctx.semantic or {}).get("understood"):
        return False
    ctx.captured_first = True
    return True


def resolve_standard(ctx: AskContext) -> bool:
    """The default matcher ladder: buffered digits → own matcher → yes/no
    from the signal → harvest answers to UPCOMING asks (capture evidence)."""
    if ctx.handled:
        return False
    node, variable, text, signal = ctx.node, ctx.variable, ctx.text, ctx.signal
    if ctx.buffered and ctx.fresh:
        ctx.value = _extract_ask_value(node, variable, ctx.combined)
        ctx.accumulated = ctx.value is not None
    guarded_free_text = ctx.guarded and _ask_is_free_text(node, variable)
    if ctx.value is None and not guarded_free_text and not ctx.joint_vars:
        ctx.value = _extract_ask_value(node, variable, text)
    if ctx.value is None and not guarded_free_text and not ctx.joint_partial:
        ctx.value = _yes_no_from_signal(node, variable, signal)
        if ctx.value is not None:
            ctx.audit.append({"action": "signal_answer", "node": ctx.node_id, "signal": signal})
    if ctx.value is None and signal is not None:
        # The label says "not an answer", yet the words may still carry
        # answers to UPCOMING asks ("guard ko diya tha, refund kab milega?"):
        # harvest them now so an off-script turn never throws them away. At a
        # FREE-TEXT ask such a capture is also the proof that the caller was
        # answering this very question — the LLM called a partner's deduction
        # story 'complaint' (cv_3fc5b4c31fe0) and the flow parked off-script,
        # lost the next answer too and let the model invent a recipient.
        # Store the narrative and move on.
        before = len(ctx.audit)
        _apply_also_capture(node, text, ctx.slots, ctx.audit, ctx.node_id)
        ctx.captured_first = True
        if guarded_free_text and (ctx.semantic_answers or any(
            entry.get("action") in _HUB_CAPTURE_ACTIONS for entry in ctx.audit[before:]
        )):
            ctx.audit.append({"action": "capture_evidence", "node": ctx.node_id, "signal": signal})
            ctx.value = text.strip() or None
    return True


# ── 3. outcome stages ────────────────────────────────────────────────────────
# Each returns True when it decided the turn.

def outcome_handled(ctx: AskContext) -> bool:
    return ctx.handled  # replied above; the ask stays open


def outcome_filled(ctx: AskContext) -> bool:
    if ctx.value is None:
        return False
    ctx.slots[ctx.variable] = ctx.value
    ctx.pending_digits.pop(ctx.node_id, None)
    ctx.node_retries.pop(ctx.node_id, None)
    entry = {"action": "slot_filled", "node": ctx.node_id, "variable": ctx.variable}
    if ctx.accumulated:
        entry["accumulated_digits"] = len(ctx.combined)
    ctx.audit.append(entry)
    if not ctx.captured_first:
        _apply_also_capture(ctx.node, ctx.text, ctx.slots, ctx.audit, ctx.node_id)
    ctx.advance()
    return True


def outcome_joint_partial(ctx: AskContext) -> bool:
    """The partner answered the OTHER half explicitly and said nothing about
    this one: leave it Unknown and move on — the flow's single ask for this
    variable follows."""
    if not ctx.joint_partial:
        return False
    ctx.audit.append({"action": "joint_partial_answer", "node": ctx.node_id, "variable": ctx.variable})
    ctx.pending_digits.pop(ctx.node_id, None)
    ctx.node_retries.pop(ctx.node_id, None)
    ctx.advance()
    return True


def outcome_digits_overflow(ctx: AskContext) -> bool:
    """Impossible buffer: longer than every length this identifier can take,
    and no matcher accepted it. Keep a separately-plausible fresh chunk as
    the new candidate, drop the rest, explain once — and burn a retry so a
    caller stuck in overflow escalates through the normal ladder."""
    if not (ctx.dictated and ctx.fresh and len(ctx.combined) > ctx.max_digits):
        return False
    ctx.pending_digits.pop(ctx.node_id, None)
    kept = ctx.fresh if len(ctx.fresh) <= ctx.max_digits else ""
    if kept:
        ctx.pending_digits[ctx.node_id] = kept
    ctx.node_retries[ctx.node_id] = ctx.node_retries.get(ctx.node_id, 0) + 1
    ctx.audit.append({"action": "identifier_overflow", "node": ctx.node_id,
                      "dropped_digits": len(ctx.combined) - len(kept), "held_digits": len(kept)})
    ctx.replies.append(canned("wf_digits_overflow", ctx.lang))
    ctx.stay()
    return True


def outcome_digits_partial(ctx: AskContext) -> bool:
    """A partial identifier: hold what was heard, keep the ask open, and do
    not burn a retry — the caller is making progress, not failing to answer."""
    if not (ctx.dictated and ctx.fresh and ctx.combined != ctx.buffered):
        return False
    ctx.pending_digits[ctx.node_id] = ctx.combined
    ctx.audit.append({"action": "digits_partial", "node": ctx.node_id, "held_digits": len(ctx.combined)})
    ctx.replies.append(canned("wf_digits_partial_count", ctx.lang).replace("{count}", str(len(ctx.combined))))
    ctx.stay()
    return True


def outcome_semantic_reask(ctx: AskContext) -> bool:
    """Unknown/unintelligible answers at a semantic-slots ask stay on the
    exact missing question, including provider failures."""
    semantic = ctx.semantic or {}
    if not ((ctx.semantic_ask and (semantic.get("failed") or ctx.signal not in _SEMANTIC_DEFERRED_SIGNALS))
            or (ctx.narrative_ask and not ctx.semantic_answers)):
        return False
    ctx.replies.append(ctx.ask_question(ctx.node, not ctx.semantic_answers, ctx.lang))
    ctx.stay()
    return True


def outcome_signal_unmatched(ctx: AskContext) -> bool:
    """The caller said something meaningful that the ask's matcher did not
    extract (hardship, complaint, a question — or a bare "ठीक है" at a choice
    question). Do not burn a retry, advance, or speak a canned "didn't catch
    that"; the brain answers in context and the ask still accepts the next
    answer — unless the node authored a fixed ``unmatchedReply``."""
    if ctx.signal is None:
        return False
    fixed_reply = ("" if ctx.semantic_ask and ctx.signal in _SEMANTIC_DEFERRED_SIGNALS
                   else ctx.unmatched_reply(ctx.node, ctx.signal, ctx.lang))
    if fixed_reply:
        ctx.audit.append({"action": "unmatched_fixed", "node": ctx.node_id, "signal": ctx.signal})
        ctx.replies.append(fixed_reply)
    else:
        ctx.audit.append({"action": "off_script", "node": ctx.node_id, "signal": ctx.signal})
        ctx.off_script = True
    ctx.stay()
    return True


def outcome_captured_other_field(ctx: AskContext) -> bool:
    """No signal and no own value, but the words answered a LATER question
    ("nahi, kuch nahi bataya tha" while the deducted amount is pending): keep
    the capture, do not burn a retry or speak "didn't catch that" — the brain
    re-asks the pending question in context (cv_e31d7ca6470b)."""
    if not _captures_other_field(ctx.node, ctx.text, ctx.slots, ctx.audit, ctx.node_id,
                                 already=ctx.captured_first):
        return False
    ctx.audit.append({"action": "off_script", "node": ctx.node_id, "signal": None,
                      "reason": "captured_other_field"})
    ctx.off_script = True
    ctx.stay()
    return True


def outcome_retry(ctx: AskContext) -> bool:
    """Nothing usable: re-ask (short form) up to the behaviour's retry budget,
    then take the authored fallback/handoff edge or hand over."""
    retries = ctx.node_retries.get(ctx.node_id, 0) + 1
    ctx.node_retries[ctx.node_id] = retries
    if retries > ctx.behavior.max_ask_retries:
        target = ctx.fallback_target(ctx.node_id)
        if target is not None:
            ctx.current, ctx.awaiting = target, None
        else:
            ctx.status = "handoff"
            ctx.replies.append(canned("wf_handover", ctx.lang))
            ctx.current, ctx.awaiting = None, None
    else:
        ctx.replies.append(ctx.ask_question(ctx.node, True, ctx.lang))
        ctx.stay()
    return True


# ── registry + default order ─────────────────────────────────────────────────

_RESOLVERS: dict[str, Stage] = {
    "joint_yes_no": resolve_joint_yes_no,
    "semantic_slots": resolve_semantic_slots,
    "narrative_guard": resolve_narrative_guard,
    "standard": resolve_standard,
}
DEFAULT_RESOLVERS: tuple[str, ...] = ("joint_yes_no", "semantic_slots", "narrative_guard", "standard")
PRE_HANDLERS: tuple[Stage, ...] = (digits_restart, digits_readback)
OUTCOMES: tuple[Stage, ...] = (
    outcome_handled, outcome_filled, outcome_joint_partial, outcome_digits_overflow,
    outcome_digits_partial, outcome_semantic_reask, outcome_signal_unmatched,
    outcome_captured_other_field, outcome_retry,
)


def register_resolver(name: str, stage: Stage) -> None:
    """Extensions add a named value resolver a node can list in ``askResolvers``."""
    _RESOLVERS[name] = stage


def resolver_names() -> tuple[str, ...]:
    return tuple(_RESOLVERS)


def resolvers_for(node: dict) -> tuple[Stage, ...]:
    """The node's ``askResolvers`` (known names only, in the given order) or
    the default chain."""
    wanted = _node_config(node).get("askResolvers")
    if isinstance(wanted, list) and wanted:
        chosen = [_RESOLVERS[str(n)] for n in wanted if str(n) in _RESOLVERS]
        if chosen:
            return tuple(chosen)
    return tuple(_RESOLVERS[n] for n in DEFAULT_RESOLVERS)


def resolve_ask(ctx: AskContext) -> AskContext:
    """Run the pipeline for one ask-node turn; the decision is on the context."""
    reconcile_question_label(ctx)
    prepare_digits(ctx)
    for handler in PRE_HANDLERS:
        if handler(ctx):
            ctx.handled = True
            break
    prepare_dictation(ctx)
    for resolver in resolvers_for(ctx.node):
        if resolver(ctx):
            break
    for outcome in OUTCOMES:
        if outcome(ctx):
            break
    return ctx
