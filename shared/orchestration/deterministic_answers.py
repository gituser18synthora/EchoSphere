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
ANSWER_KINDS = frozenset({
    ASK_KIND_YES_NO, ASK_KIND_CHOICE, ASK_KIND_NUMBER, ASK_KIND_DATE, ASK_KIND_TIME,
})

# Resolver names recorded on decision_skipped_deterministic.
RESOLVER_YES_NO = "yes_no_lexicon"
RESOLVER_CHOICE = "ask_resolution"
RESOLVER_NUMBER = "spoken_numbers"
RESOLVER_DATETIME = "visit_datetime"

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


def _choice(text: str, options: tuple[str, ...], question: str) -> str | None:
    if not options:
        return None
    picked = positional_choice(text, {"allowedValues": list(options)}, question)
    if picked is not None:
        return picked
    # The option itself, said alone (fillers / courtesy / a leading yes
    # aside): "पहला" is positional above; "evening" / "शाम" match only an
    # option spelled that way — synonyms stay with the workflow's matcher.
    content = [t for t in _tokens(text) if t not in _NEUTRAL_WORDS and t not in _YES_WORDS]
    if not content:
        return None
    said = " ".join(content)
    matches = [o for o in options if " ".join(_tokens(o)) == said]
    return matches[0] if len(matches) == 1 else None


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
) -> DeterministicAnswer | None:
    """The deterministic reading of ``text`` as the answer to a pending ask of
    ``kind``, or None when the reply needs the decision LLM."""
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
    if yes_no_polarity(stripped) == "no":
        # "नहीं" at a choice / number / date ask refuses it; the model reads
        # what the caller means instead.
        return None
    if kind == ASK_KIND_CHOICE:
        value = _choice(stripped, options, question)
        return DeterministicAnswer(kind, RESOLVER_CHOICE, value) if value else None
    if kind == ASK_KIND_NUMBER:
        value = _number(stripped)
        return DeterministicAnswer(kind, RESOLVER_NUMBER, value) if value else None
    value = _day(stripped, timezone) if kind == ASK_KIND_DATE else _time(stripped)
    return DeterministicAnswer(kind, RESOLVER_DATETIME, value) if value else None
