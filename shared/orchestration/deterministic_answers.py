"""Short answers the runtime resolves without the decision LLM.

The Goal Engine decision sits serially in front of every reply (live demo
2026-10-27: 1.4–1.9 s on every turn, ~1.6 s of a scripted bot's 2.0–2.4 s
response). A large share of turns answer a closed question with one to three
words — "हाँ जी", "नहीं", "पहला वाला", "कल", "शाम पाँच बजे" — and the platform
already owns deterministic readers for exactly those: the yes/no lexicon,
ask resolution (options, ordinals, deictics), spoken numbers and the visit
date/time parser. This module decides whether such a reply is unambiguous
enough to stand in for the decision.

The rule is deliberately narrow — every condition must hold, else the turn
goes to the decision LLM as before:

- the reply has at most :data:`MAX_WORDS` words and no question shape;
- the regex signal bank finds nothing but a plain yes/no in it (no hold,
  callback, complaint, agent request, wrong person, hardship, …);
- one resolver reads it, and every remaining word is a filler or courtesy
  word — an unknown content word ("नहीं पता", "हाँ बोलो") means the reply
  carries more than the answer;
- a yes/no reply has one polarity: "हाँ नहीं" / "हाँ ना" stay with the model
  (the courtesy "जी" goes with either: "जी नहीं" is a no);
- a bare "hmm" is not a yes here: on the recorded demo decisions the model
  read "हम्म" at a yes/no ask as a request to clarify.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from shared.orchestration import lang as _lang
from shared.orchestration.ask_resolution import (
    ASK_KIND_CHOICE,
    ASK_KIND_DATE,
    ASK_KIND_NUMBER,
    ASK_KIND_TIME,
    ASK_KIND_YES_NO,
    positional_choice,
)
from shared.orchestration.router import classify_user_signal, looks_like_question
from shared.orchestration.spoken_numbers import is_number_word, verbalized_digits
from shared.orchestration.visit_datetime import (
    extract_day,
    extract_time,
    fold_text,
    now_in,
    parse_day,
    parse_time,
)

MAX_WORDS = 4
# A paused workflow INTENT hub, read through its edges (``edges``): a yes/no
# needs an edge carrying that signal; an option-like reply must equal one
# edge's authored token ("aaj", "wireline", "pata nahi").
ASK_KIND_HUB = "hub"
ANSWER_KINDS = frozenset({
    ASK_KIND_YES_NO, ASK_KIND_CHOICE, ASK_KIND_NUMBER, ASK_KIND_DATE, ASK_KIND_TIME,
    ASK_KIND_HUB,
})

# Resolver names recorded on decision_skipped_deterministic.
RESOLVER_YES_NO = "yes_no_lexicon"
RESOLVER_CHOICE = "ask_resolution"
RESOLVER_NUMBER = "spoken_numbers"
RESOLVER_DATETIME = "visit_datetime"
RESOLVER_EDGE = "workflow_edge"
# Signals a yes / no at a hub may take (the engine's compatible pairs).
_YES_EDGE_SIGNALS = frozenset({"affirm", "payment_intent"})
_NO_EDGE_SIGNALS = frozenset({"refusal"})
_PHRASE_PUNCT = ".,!?;:\"'()[]-" + "।॥"

SIGNAL_YES = "affirm"
SIGNAL_NO = "refusal"

_YES_WORDS = _lang.union("bare_yes")
_NO_WORDS = _lang.union("bare_no")
# "जी" is courtesy as much as assent: alone it is a yes, next to a no word it
# is the polite form of that no ("जी नहीं").
_POLITE_WORDS = frozenset({"ji", "jee", "जी"})
_COURTESY_WORDS = frozenset({
    "sir", "madam", "mam", "maam", "ma'am", "सर", "मैडम", "मैम", "मेम",
})
_NEUTRAL_WORDS = _lang.union("fillers") | _COURTESY_WORDS | _POLITE_WORDS
_TOKEN_SPLIT = re.compile(
    r"[\s,;:\-\"()\[\]" + re.escape(_lang.sentence_terminators()) + r"]+"
)
# Postpositions that may follow a day or time ("कल को", "पाँच बजे को").
_DATETIME_PARTICLES = frozenset({"ko", "को"})
# The regex signals a short answer may carry; any other signal (hold,
# callback, complaint, agent request, wrong person, hardship, question, …)
# is more than an answer.
_ANSWER_SIGNALS = frozenset({None, SIGNAL_YES, SIGNAL_NO})


@dataclass(frozen=True)
class DeterministicAnswer:
    """One resolved short reply: the kind of ask it answers, the resolver
    that read it, the value (yes / no, the option, digits, the day or time
    as said) and the platform signal it carries (yes/no answers only)."""

    kind: str
    resolver: str
    value: str
    signal: str | None = None


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_SPLIT.split(text or "") if t.strip("'.")]


def _only_neutral(tokens: list[str], extra: frozenset[str] = frozenset()) -> bool:
    return all(t in _NEUTRAL_WORDS or t in extra for t in tokens)


def yes_no_polarity(text: str) -> str | None:
    """"yes" / "no" when every word is a yes/no, filler or courtesy word and
    only one polarity is present; None otherwise."""
    yes = no = polite = False
    for token in _tokens(text):
        if token in _NO_WORDS:
            no = True
        elif token in _POLITE_WORDS:
            polite = True
        elif token in _YES_WORDS:
            yes = True
        elif token in _NEUTRAL_WORDS:
            continue
        else:
            return None
    if no:
        return None if yes else "no"
    return "yes" if (yes or polite) else None


def _phrase(text: str) -> str:
    """The utterance as one comparable phrase: lower-cased, punctuation off,
    one space between words."""
    return " ".join(
        t for t in (w.strip(_PHRASE_PUNCT).lower() for w in (text or "").split()) if t
    )


def _content(text: str) -> str:
    """The reply minus fillers, courtesy words and yes words — what names an
    option ("haan, guard ko" → "guard ko")."""
    return " ".join(
        t for t in _tokens(text) if t not in _NEUTRAL_WORDS and t not in _YES_WORDS
    )


def _choice(
    text: str, options: tuple[str, ...], question: str,
    synonyms: dict[str, list[str]] | None = None,
) -> str | None:
    if not options:
        return None
    picked = positional_choice(text, {"allowedValues": list(options)}, question)
    if picked is not None:
        return picked
    # The option itself, or one of its authored surfaces, said alone
    # (fillers / courtesy / a leading yes aside): "पहला" is positional above.
    said = _content(text)
    if not said:
        return None
    matches = [o for o in options if _phrase(o) == said]
    for option, surfaces in (synonyms or {}).items():
        if option in options and option not in matches and any(_phrase(x) == said for x in surfaces):
            matches.append(option)
    return matches[0] if len(matches) == 1 else None


def _phrase_has_token(phrase: str, token: str) -> bool:
    """Whether an edge token occurs in the phrase at a word start (the
    engine's literal match, ``_token_in``)."""
    token = _phrase(token)
    if not token:
        return False
    at = phrase.find(token)
    while at != -1:
        if at == 0 or phrase[at - 1] == " ":
            return True
        at = phrase.find(token, at + 1)
    return False


def _hub(text: str, edges: list[dict]) -> DeterministicAnswer | None:
    """A reply at an intent hub: a plain yes / no when an edge carries that
    signal, else the one edge whose authored token the reply IS — the engine
    then takes that edge on the literal match (signal None), as it would
    after a decision that named no signal."""
    polarity = yes_no_polarity(text)
    if polarity is not None:
        wanted = _YES_EDGE_SIGNALS if polarity == "yes" else _NO_EDGE_SIGNALS
        if any(wanted & set(e.get("signals") or ()) for e in edges):
            return DeterministicAnswer(
                ASK_KIND_HUB, RESOLVER_YES_NO, polarity,
                SIGNAL_YES if polarity == "yes" else SIGNAL_NO,
            )
        return None
    said = _content(text)
    if not said:
        return None
    exact = [
        index for index, e in enumerate(edges)
        if any(_phrase(t) == said for t in e.get("tokens") or ())
    ]
    if len(exact) != 1:
        return None
    # No other edge may read a token inside the reply ("aaj sham" names two
    # edges' tokens on a time hub): the engine's longest-token rule would then
    # decide, not the caller.
    others = [
        e for index, e in enumerate(edges) if index != exact[0]
        and any(_phrase_has_token(said, t) for t in e.get("tokens") or ())
    ]
    if others:
        return None
    return DeterministicAnswer(ASK_KIND_HUB, RESOLVER_EDGE, said)


def _number(text: str) -> str | None:
    tokens = _tokens(text)
    numeric = [t for t in tokens if is_number_word(t)]
    if not numeric or not _only_neutral([t for t in tokens if not is_number_word(t)]):
        return None
    digits = verbalized_digits(" ".join(numeric)).split()
    return digits[0] if len(digits) == 1 and digits[0].isdigit() else None


def _residue(text: str, span: str) -> list[str]:
    folded = fold_text(text)
    at = folded.find(span)
    if at < 0:
        return ["?"]
    return _tokens(folded[:at] + " " + folded[at + len(span):])


def _day(text: str, timezone: str | None) -> str | None:
    span = extract_day(text)
    if not span or not _only_neutral(_residue(text, span), _DATETIME_PARTICLES):
        return None
    return span if parse_day(span, now_in(timezone).date()).kind == "ok" else None


def _time(text: str) -> str | None:
    span = extract_time(text)
    if not span or not _only_neutral(_residue(text, span), _DATETIME_PARTICLES):
        return None
    return span if parse_time(span).kind == "exact" else None


def resolve_short_answer(
    text: str,
    kind: str,
    *,
    options: tuple[str, ...] = (),
    question: str = "",
    timezone: str | None = None,
    synonyms: dict[str, list[str]] | None = None,
    edges: list[dict] | None = None,
) -> DeterministicAnswer | None:
    """The deterministic reading of ``text`` as the answer to a pending ask of
    ``kind``, or None when the reply needs the decision LLM. ``synonyms`` are
    a choice ask's authored surfaces per option; ``edges`` a hub's edges
    (``{"tokens", "signals"}``, kind ``hub``)."""
    if kind not in ANSWER_KINDS:
        return None
    stripped = (text or "").strip()
    words = _tokens(stripped)
    if not words or len(words) > MAX_WORDS:
        return None
    if "?" in stripped or looks_like_question(stripped):
        return None
    if classify_user_signal(stripped) not in _ANSWER_SIGNALS:
        return None
    if kind == ASK_KIND_YES_NO:
        polarity = yes_no_polarity(stripped)
        if polarity is None:
            return None
        return DeterministicAnswer(
            kind, RESOLVER_YES_NO, polarity,
            SIGNAL_YES if polarity == "yes" else SIGNAL_NO,
        )
    if kind == ASK_KIND_HUB:
        return _hub(stripped, list(edges or ()))
    if yes_no_polarity(stripped) == "no":
        # "नहीं" at a choice / number / date ask refuses it; the model reads
        # what the caller means instead.
        return None
    if kind == ASK_KIND_CHOICE:
        value = _choice(stripped, options, question, synonyms)
        return DeterministicAnswer(kind, RESOLVER_CHOICE, value) if value else None
    if kind == ASK_KIND_NUMBER:
        value = _number(stripped)
        return DeterministicAnswer(kind, RESOLVER_NUMBER, value) if value else None
    value = _day(stripped, timezone) if kind == ASK_KIND_DATE else _time(stripped)
    return DeterministicAnswer(kind, RESOLVER_DATETIME, value) if value else None
