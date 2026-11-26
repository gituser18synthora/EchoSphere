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
# A contact check, thanks or a goodbye at a hub that declares answer options:
# no answer to judge, the hub asks its question again.
RESOLVER_REACTION = "reaction"
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
}) | _lang.union("courtesy_words")
_NEUTRAL_WORDS = (
    _lang.union("fillers") | _COURTESY_WORDS | _POLITE_WORDS | _lang.union("lead_in_words")
)
# "bilkul" strengthens whichever answer it stands next to: "बिल्कुल" is a yes,
# "बिल्कुल नहीं" a no.
_INTENSIFIER_WORDS = _lang.union("intensifier_words")
_AGREEMENT_WORDS = _lang.union("agreement_words")
# "है" after a yes word ("ठीक है", "सही है") adds nothing to it.
_COPULA_WORDS = frozenset({"hai", "hain", "है", "हैं"})
# Replies that are a reaction, not an answer. A contact check ("hello",
# "hmm", "haan boliye"), thanks and a goodbye never answer a business
# question: no permission, no refusal, no option.
_ATTENTION_WORDS = _lang.union("attention_words")
_CONTACT_WORDS = _lang.union("contact_words")
_THANKS_WORDS = _lang.union("thanks_words")
_FAREWELL_WORDS = _lang.union("farewell_words")
_REACTION_MAX_WORDS = 6
# Multi-word reactions, folded to one category word before the word check.
_REACTION_PHRASES = (
    (re.compile(
        r"\bgood\s*(?:morning|afternoon|evening)\b|गुड\s*(?:मॉर्निंग|मार्निंग|इवनिंग|ईवनिंग|आफ्टरनून)",
        # a returned greeting: attention, but not a contact check
        re.I), " namaste "),
    (re.compile(
        r"\bthank\s*(?:you|u)\b(?:\s+(?:so|very)\s+much)?|\bthanks\s+a\s+lot\b"
        r"|थैंक\s*(?:यू|यु|ऊ)(?:\s+(?:सो|वेरी)\s+मच)?|बहुत[\s-]*बहुत\s+(?=धन्यवाद|शुक्रिया)|बहुत\s+(?=धन्यवाद|शुक्रिया)",
        re.I), " thanks "),
    (re.compile(
        r"\bgood\s*(?:night|day|bye)\b|\bbye[\s-]*bye\b|\bsee\s+you(?:\s+(?:later|soon|again))?\b"
        r"|\btake\s+care\b|गुड\s*(?:नाइट|डे|बाय)|बाय[\s-]*बाय|शुभ\s+रात्रि|फिर\s+मिलते\s+हैं|फिर\s+मिलेंगे",
        re.I), " bye "),
)
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


def yes_no_polarity(
    text: str, *, agreement: bool = True, with_thanks: bool = False,
) -> str | None:
    """"yes" / "no" when every word is a yes/no, filler or courtesy word and
    only one polarity is present; None otherwise.

    ``agreement=False`` is for a question that asks about a STATE rather than
    proposing something: "ठीक है" / "सही है" / "ok" / "fine" there may describe
    the state ("is there still a problem?" — "ठीक है" means it works), so a
    reply containing such a word is not read as a plain yes.

    ``with_thanks=True`` is for a question that declares answer options:
    "ठीक है, थैंक यू" is then still a yes and "no, thank you" still a no.
    Elsewhere thanks or a goodbye beside the word keeps the reply out of this
    deterministic reading ("okay, thank you" after "anything else?" declines),
    exactly as before."""
    yes = no = polite = intensifier = thanks = False
    for token in _reaction_tokens(text):
        if not agreement and token in _AGREEMENT_WORDS:
            return None
        if not with_thanks and (token in _THANKS_WORDS or token in _FAREWELL_WORDS):
            return None
        if token in _NO_WORDS:
            no = True
        elif token in _POLITE_WORDS:
            polite = True
        elif token in _INTENSIFIER_WORDS:
            intensifier = True
        elif token in _YES_WORDS:
            yes = True
        elif token in _THANKS_WORDS or token in _FAREWELL_WORDS:
            # "ठीक है, थैंक यू" is still a yes and "no, thank you" still a no.
            thanks = True
        elif token in _NEUTRAL_WORDS:
            continue
        else:
            return None
    if no:
        return None if yes else "no"
    if yes or intensifier:
        return "yes"
    # The courtesy "जी" alone is a yes; beside thanks or a goodbye it is only
    # the polite form of that ("जी, धन्यवाद").
    return "yes" if polite and not thanks else None


def bare_yes_no_token(token: str) -> bool:
    """An authored edge token that is nothing but yes/no words ("नहीं", "ठीक
    है", "जी हाँ"). A phrase that merely contains one ("अब ठीक है", "कोई विजिट
    नहीं", "पैसे नहीं") is a content token: it keeps deciding where a bare
    yes/no word inside a longer reply does not."""
    words = _tokens(token)
    if not words or len(token or "") < 3:
        return True
    plain = _YES_WORDS | _NO_WORDS | _POLITE_WORDS | _COURTESY_WORDS | _COPULA_WORDS
    return all(word in plain for word in words)


def _reaction_tokens(text: str) -> list[str]:
    folded = text or ""
    for pattern, word in _REACTION_PHRASES:
        folded = pattern.sub(word, folded)
    return _tokens(folded)


def attention_only(text: str) -> bool:
    """A contact check with no answer in it: "hello", "हेलो हेलो", "hmm",
    "हाँ बोलिए", "जी बताइए", "good morning", and a questioning "हाँ?" /
    "जी?". A leading yes word does not turn it into an answer — "हाँ, हेलो"
    is still "are you there?"."""
    tokens = _reaction_tokens(text)
    if not tokens or len(tokens) > _REACTION_MAX_WORDS:
        return False
    plain = _NEUTRAL_WORDS | _YES_WORDS | _INTENSIFIER_WORDS
    if not all(t in plain or t in _ATTENTION_WORDS for t in tokens):
        return False
    return any(t in _ATTENTION_WORDS for t in tokens) or "?" in (text or "")


def without_stray_fragments(text: str) -> str:
    """``text`` without the one- or two-character tokens that belong to no
    word category: what speech-to-text adds around a short phrase heard over
    other audio ("हाँ जी, बोलिए। के?"). Known short words (जी, ना, no, ok …)
    stay; so does everything longer."""
    known = (_NEUTRAL_WORDS | _YES_WORDS | _NO_WORDS | _INTENSIFIER_WORDS | _ATTENTION_WORDS
             | _THANKS_WORDS | _FAREWELL_WORDS)
    kept = []
    for raw in (text or "").split():
        core = "".join(_tokens(raw))
        if core and len(core) <= 2 and core not in known:
            continue
        kept.append(raw)
    return " ".join(kept)


def contact_check_only(text: str) -> bool:
    """The part of :func:`attention_only` that may cut the bot's speech with a
    single word: "hello?", "हेलो हेलो", "सुनिए" — the caller cannot hear the
    bot or wants the floor. "बोलिए" / "बताइए", a hum and a returned greeting
    invite the bot to go on; they are no reason to stop it mid-sentence."""
    return attention_only(text) and any(t in _CONTACT_WORDS for t in _reaction_tokens(text))


def thanks_only(text: str) -> bool:
    """Thanks or a goodbye with no answer in it: "thank you", "धन्यवाद",
    "bye". With a yes or no word beside it the reply is that answer said
    politely ("ठीक है, थैंक यू"), not thanks alone."""
    tokens = _reaction_tokens(text)
    if not tokens or len(tokens) > _REACTION_MAX_WORDS:
        return False
    core = _THANKS_WORDS | _FAREWELL_WORDS
    return (
        all(t in core or t in _NEUTRAL_WORDS for t in tokens)
        and any(t in core for t in tokens)
        and yes_no_polarity(text, with_thanks=True) is None
    )


def reaction_only(text: str) -> bool:
    """A reply that reacts to the bot without answering its question."""
    return attention_only(text) or thanks_only(text)


# What a caller says back to a goodbye without asking for anything more.
_CLOSING_EXTRA_WORDS = frozenset({
    "accha", "acha", "achha", "achcha", "अच्छा", "ओके", "ok", "okay", "bas", "बस",
    "theek", "thik", "ठीक", "hai", "है", "phir", "फिर", "milte", "मिलते", "hain", "हैं",
    "aapka", "आपका", "aapko", "आपको", "bhi", "भी", "din", "दिन", "shubh", "शुभ", "rahe", "रहे",
    "same", "to", "too", "you", "aap", "आप", "namaste", "नमस्ते", "नमस्कार", "namaskar",
    "jai", "जय", "hind", "हिंद", "ram", "राम", "shri", "श्री", "krishna", "कृष्ण", "radhe", "राधे",
})
_CLOSING_REACTION_MAX_WORDS = 8


def closing_reaction(text: str) -> bool:
    """A content-free reaction to a farewell: an acknowledgement ("ठीक है",
    "ok ji", "अच्छा", "हाँ जी मैम"), thanks, a goodbye, a greeting returned
    ("आपका भी दिन शुभ रहे", "जय श्री राम"), a contact check or a hum. A no,
    a question or any content word makes it something to answer."""
    if attention_only(text) or thanks_only(text):
        return True
    tokens = _reaction_tokens(text)
    if not tokens or len(tokens) > _CLOSING_REACTION_MAX_WORDS or "?" in (text or ""):
        return False
    allowed = (_NEUTRAL_WORDS | _YES_WORDS | _INTENSIFIER_WORDS | _ATTENTION_WORDS
               | _THANKS_WORDS | _FAREWELL_WORDS | _CLOSING_EXTRA_WORDS)
    return all(t in allowed for t in tokens)


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


def _hub(text: str, edges: list[dict], *, with_thanks: bool = False) -> DeterministicAnswer | None:
    """A reply at an intent hub: a plain yes / no when an edge carries that
    signal, else the one edge whose authored token the reply IS — the engine
    then takes that edge on the literal match (signal None), as it would
    after a decision that named no signal. ``with_thanks`` is the reading of
    a hub that declares answer options (see :func:`yes_no_polarity`)."""
    polarity = yes_no_polarity(text, with_thanks=with_thanks)
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


def resolve_plain_yes_no(
    text: str, edges: list[dict], *, agreement: bool = True,
) -> DeterministicAnswer | None:
    """A reply of ANY length made of nothing but yes / no, courtesy, filler
    and thanks words ("थैंक यू मैडम, ठीक है।", "जी, जी, बिल्कुल ठीक है"), at a
    hub that declares answer options and has an edge for that answer.

    :func:`resolve_short_answer` stops at ``MAX_WORDS``; past it such a reply
    went to the decision model, which now and then asked the caller to
    clarify a plain yes. ``agreement=False`` is a question about a state
    (see :func:`yes_no_polarity`)."""
    stripped = (text or "").strip()
    if not stripped or "?" in stripped or looks_like_question(stripped):
        return None
    polarity = yes_no_polarity(stripped, agreement=agreement, with_thanks=True)
    if polarity is None:
        return None
    wanted = _YES_EDGE_SIGNALS if polarity == "yes" else _NO_EDGE_SIGNALS
    if not any(wanted & set(e.get("signals") or ()) for e in edges or ()):
        return None
    return DeterministicAnswer(
        ASK_KIND_HUB, RESOLVER_YES_NO, polarity, SIGNAL_YES if polarity == "yes" else SIGNAL_NO,
    )


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


def resolve_reaction(text: str) -> DeterministicAnswer | None:
    """The deterministic reading of a reply that only reacts ("hello", "hmm",
    "haan boliye", "thank you") at a hub with answer options: it carries no
    signal and no value, and needs no decision — the hub re-asks at once
    instead of leaving the caller in silence while a model call runs."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    if attention_only(stripped):
        return DeterministicAnswer(ASK_KIND_HUB, RESOLVER_REACTION, "attention")
    if thanks_only(stripped):
        return DeterministicAnswer(ASK_KIND_HUB, RESOLVER_REACTION, "thanks")
    return None


def resolve_short_answer(
    text: str,
    kind: str,
    *,
    options: tuple[str, ...] = (),
    question: str = "",
    timezone: str | None = None,
    synonyms: dict[str, list[str]] | None = None,
    edges: list[dict] | None = None,
    with_thanks: bool = False,
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
        return _hub(stripped, list(edges or ()), with_thanks=with_thanks)
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
