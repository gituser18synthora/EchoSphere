"""Reading a reply to the bot's identity question — deterministically.

``shared.orchestration.identity_confirmation`` defines the two modes; this
module applies them on a live call, before any decision LLM runs:

- :func:`classify_identity_reply` reads one caller reply as ``affirm``
  ("haan", "ji haan"), ``go_ahead`` ("haan boliye", "ji bolo"),
  ``self_identified`` ("main hi bol raha hoon", "haan main Gaurav bol raha
  hoon") or None. None covers everything else — negations, "galat number",
  someone speaking for the person ("main unka bhai bol raha hoon"), a
  different name, a question back, anything longer or unknown — and those
  stay with the decision LLM (and the regex fallback) exactly as before;
- :func:`identity_reply_outcome` turns that reading into ``confirm`` or
  ``explicit_reask`` (strict mode, a go-ahead before the explicit question);
- the re-ask ladder (:func:`identity_reask`) words every identity re-ask
  differently, and :class:`IdentityConfirmation` counts how often each
  wording was spoken so the same identity question is never asked a third
  time (cv_6d46f8de094d: Manappuram asked "क्या मेरी बात गौरव जी से हो रही
  है?" three times after "हाँ जी बोलिए" and "हाँ बोलिए").
"""

from __future__ import annotations

import re
from collections import Counter

from shared.orchestration.identity_confirmation import STRICT
from shared.orchestration.phrases import resolve_phrase
from voice_runtime.spoken_text import normalize_word, normalize_words

# Reply kinds (see classify_identity_reply) and rule outcomes.
AFFIRM = "affirm"
GO_AHEAD = "go_ahead"
SELF_IDENTIFIED = "self_identified"
CONFIRM = "confirm"
EXPLICIT_REASK = "explicit_reask"

# Whether the bot's utterance asks who it is speaking with. Shared with the
# collections policy (which arms awaiting_identity on it).
IDENTITY_QUESTION = re.compile(
    r"(?:baat|बात)[^।?!]{0,50}(?:ho rah|kar rah|हो रह|कर रह)"
    r"|am i speaking|speaking (?:with|to)|is (?:this|that)\s+\S+"
    r"|account holder|अकाउंट होल्डर"
    r"|(?:aap|आप)[^।?!]{0,20}(?:hi|ही)\s*(?:bol|बोल)"
    # The explicit re-asks: "क्या आप ही गौरव जी बोल रहे हैं?", "क्या आप
    # गौरव जी हैं?", "आप गौरव जी ही हैं न?".
    r"|(?:aap|आप)\s*(?:hi|ही)\s[^।?!]{0,40}(?:bol rah|बोल रह)"
    r"|(?:kya|क्या)\s+(?:aap|आप)\s+[^।?!]{1,40}?(?:ji|जी)\s+(?:(?:hi|ही)\s+)?(?:hain|हैं)"
    r"|(?:aap|आप)\s+[^।?!]{1,40}?(?:ji|जी)\s+(?:hi|ही)\s+(?:hain|हैं)"
    # Malayalam / Tamil greeting forms ("ഞാൻ X ജിയോടാണോ സംസാരിക്കുന്നത്?",
    # "X തന്നെയാണോ?", "நான் X உடன் பேசுகிறேனா?", "X தானா பேசுவது?"). Without
    # these the policy never armed awaiting_identity on a Malayalam greeting,
    # so no identity/refusal/hardship fast path ever fired for such callers.
    r"|(?:ഓടാണോ|യോടാണോ|ജിയോടാണോ|ആണോ)\s*സംസാരിക്കുന്നത്|സംസാരിക്കുന്നത്[^?]{0,30}ആണോ"
    r"|തന്നെയാണോ|ആണോ\s*(?:ഞാൻ\s*)?സംസാരിക്കുന്ന"
    # LLM-authored re-asks (seen on a memory-continued call where the greeting
    # carried no identity question): "are you Gaurav Pandey?", "നിങ്ങൾ ഗൗരവ്
    # ആണോ എന്ന്…", "…എന്നുള്ളത് ശരിയാണോ", "ദയവായി സ്ഥിരീകരിക്കുക — നിങ്ങൾ …",
    # "நீங்கள் … தானா". Without these the gate never arms and the caller's
    # yes is judged as a general turn instead of an identity answer.
    r"|\bare you\s+(?!able|sure|there|okay|ok|still|ready|calling)[A-Za-z]"
    r"|നിങ്ങൾ[^?]{0,40}(?:ആണോ|യാണോ|തന്നെയാണോ)|ആണോ\s*എന്ന്|എന്നുള്ളത്\s*ശരിയാ|സ്ഥിരീകരിക്ക[^?]{0,40}(?:ആണ്|ആണോ)"
    r"|நீங்கள்[^?]{0,40}(?:தானா|ஆ\?)"
    r"|உடன்\s*(?:தான்\s*)?பேசு|பேசுகிறேனா|பேசுறேனா|பேசுவது[^?]{0,30}(?:உடனா|தானா)|தானா\s*(?:பேசு|\?)",
    re.I,
)

# Someone answering FOR the person ("मैं उनका भाई बोल रहा हूँ", "his wife
# here"): never a confirmation, and a denial on the regex fallback path.
_REL = (
    r"bhai|bhaiya|beta|beti|behen|bahan|papa|pita|mummy|maa|mother|father"
    r"|pati|patni|biwi|wife|husband|dost|friend|son|daughter|brother|sister"
    r"|भाई|भैया|बेटा|बेटी|बहन|पापा|पिता|मम्मी|माँ|मां|पति|पत्नी|बीवी|दोस्त"
)
_POSS = (
    r"unka|unki|unke|uska|uski|uske|inka|inki|inke"
    r"|उनका|उनकी|उनके|उसका|उसकी|उसके|इनका|इनकी|इनके"
)
OTHER_PERSON = re.compile(
    rf"(?<![\wऀ-ॿ])(?:{_POSS})\s+(?:{_REL})(?![\wऀ-ॿ])"
    rf"|(?<![\wऀ-ॿ])(?:main|mai|मैं)\s+(?:{_POSS})(?![\wऀ-ॿ])"
    r"|\b(?:his|her|their)\s+(?:brother|son|daughter|sister|wife|husband"
    r"|father|mother|friend)\b|\bi am (?:his|her)\b",
    re.I,
)

# The two English alternatives above that also match ordinary questions ("Is
# this a good time?", "Are you interested?"): enough for the collections
# policy, which only listens before verification, but on their own never an
# identity question for the call tracker below.
_BROAD_IDENTITY = re.compile(r"is (?:this|that)\s+\S+|\bare you\s+", re.I)

_SPLIT = re.compile(r"[\s,.!।॥…—–\-:;\"'“”‘’()]+")
_SENTENCES = re.compile(r"(?<=[।.!?？])\s+")
_MAX_REPLY_WORDS = 10

_AFFIRM_WORDS = frozenset({
    "haan", "han", "haa", "haanji", "hanji", "haanjee", "ji", "jee", "jii",
    "yes", "yeah", "yep", "yup", "bilkul", "correct", "absolutely",
    "हाँ", "हां", "हाँजी", "हांजी", "जी", "बिल्कुल", "बिलकुल",
})
# Courtesy words that add nothing to an answer.
_COURTESY_WORDS = frozenset({
    "ji", "jee", "sir", "madam", "maam", "mam", "please", "aap",
    "जी", "सर", "मैडम", "मैम", "मेम", "प्लीज़", "प्लीज", "आप",
})
_GO_AHEAD_WORDS = frozenset({
    "boliye", "bolie", "boliyega", "bolo", "bolen", "bolein", "bataiye",
    "bataie", "batayiye", "bataiyega", "batao", "bataen", "kahiye", "kahie",
    "kaho", "suniye", "sunaiye", "suno", "aage", "goahead", "tellme",
    "continue", "proceed",
    "बोलिए", "बोलिये", "बोलिएगा", "बोलो", "बोलें", "बताइए", "बताइये",
    "बताइएगा", "बताओ", "बताएं", "बताएँ", "कहिए", "कहिये", "कहो", "सुनिए",
    "सुनाइए", "सुनो", "आगे",
})
# "बोलिए ना" — "na" is a softener after a go-ahead, a negation anywhere else.
_SOFTENERS = frozenset({"na", "ना"})
_NEGATIONS = frozenset({
    "nahi", "nahin", "nhi", "no", "not", "nope", "galat", "wrong", "mat",
    "नहीं", "नही", "नहि", "गलत", "ग़लत", "मत",
})
_QUESTION_WORDS = frozenset({
    "kaun", "kya", "kyun", "kyon", "kahan", "kab", "kaise", "who", "what",
    "why", "where", "when", "how", "hello", "helo",
    "कौन", "क्या", "क्यों", "कहाँ", "कहां", "कब", "कैसे", "हेलो", "हैलो",
})
_RELATION_WORDS = frozenset(re.split(r"\|", _REL) + re.split(r"\|", _POSS))
_HONORIFICS = frozenset({
    "ji", "jee", "sir", "madam", "mr", "mrs", "ms", "shri", "shree", "smt",
    "जी", "सर", "मैडम", "श्री", "श्रीमती",
})
# Self-identification after the leading affirmation: "main hi bol raha
# hoon", "<name> bol rahi hoon", "main hoon", "this is <name>", "speaking".
# A bare "बोल रहा" is a fragment, never an answer: the speaking form needs
# "main", or the auxiliary with a leading yes or a name ("haan bol raha
# hoon", "Gaurav bol raha hoon").
_SELF_SPEAKING = re.compile(
    r"^(?P<first>(?:main|mai|mein|मैं|मै)\s+)?(?:(?:hi|ही)\s+)?(?P<name>(?:\S+\s+){0,2}?)"
    r"(?:(?:hi|ही)\s+)?"
    r"(?:(?:bol|बोल)\s+(?:raha|rahi|rahe|रहा|रही|रहे)"
    r"|(?:baat|बात)\s+(?:kar|कर)\s+(?:raha|rahi|rahe|रहा|रही|रहे))"
    r"(?P<aux>\s+(?:hoon|hu|hun|hoo|hain|हूँ|हूं|हु|हैं))?$"
)
_SELF_BEING = re.compile(
    r"^(?:main|mai|mein|मैं|मै)\s+(?:(?:hi|ही)\s+)?(?P<name>(?:\S+\s+){0,2}?)"
    r"(?:(?:hi|ही)\s+)?(?:hoon|hu|hun|hoo|हूँ|हूं|हु)$"
    r"|^(?:main|mai|mein|मैं|मै)\s+(?:hi|ही)$"
)
_SELF_ENGLISH = re.compile(
    r"^(?:its me|it is me|this is me|speaking|me speaking)$"
    r"|^(?:this is|i am|im)\s+(?P<name>\S+(?:\s+\S+)?)(?:\s+(?:here|speaking))?$"
)
_ENGLISH_PHRASES = (
    (re.compile(r"\bgo (?:ahead|on)\b"), "goahead"),
    (re.compile(r"\btell me\b"), "tellme"),
    (re.compile(r"\b(?:it|i|you)'(?=[a-z])"), lambda m: m.group(0)[:-1]),
)


def _tokens(text: str) -> list[str]:
    lowered = (text or "").lower()
    for pattern, replacement in _ENGLISH_PHRASES:
        lowered = pattern.sub(replacement, lowered)
    return [t for t in _SPLIT.split(lowered) if t]


def _name_key(word: str) -> str:
    """Script-independent comparison key of a name word: the consonant
    skeleton of its romanized form ("Gaurav" == "गौरव" == "grv")."""
    key = normalize_word(word)
    return re.sub(r"(?<=.)[aeiouy]", "", key) or key


def subject_name_keys(subject_name: str) -> frozenset[str]:
    return frozenset(
        _name_key(w) for w in (subject_name or "").split()
        if w.lower() not in _HONORIFICS and _name_key(w)
    )


def _names_subject(name_slot: str, subject_keys: frozenset[str]) -> bool:
    """Whether a stated name is (part of) the subject's name. An empty slot
    ("main hi bol raha hoon") names nobody else and passes."""
    words = [w for w in name_slot.split() if w not in _HONORIFICS]
    if not words:
        return True
    if not subject_keys:
        return False
    return all(_name_key(w) in subject_keys for w in words)


def _self_identified(
    rest: list[str], subject_keys: frozenset[str], affirm_led: bool
) -> bool | None:
    """True for a self-identification naming nobody else, False when it names
    someone else, None when ``rest`` is not a self-identification at all."""
    phrase = " ".join(rest)
    for pattern in (_SELF_SPEAKING, _SELF_BEING, _SELF_ENGLISH):
        match = pattern.match(phrase)
        if not match:
            continue
        groups = match.groupdict()
        name = (groups.get("name") or "").strip()
        if pattern is _SELF_SPEAKING and not groups.get("first") and not (
            groups.get("aux") and (affirm_led or name)
        ):
            return None
        return _names_subject(name, subject_keys)
    return None


def classify_identity_reply(text: str, subject_name: str = "") -> str | None:
    """``affirm`` / ``go_ahead`` / ``self_identified`` or None (see module doc).

    Only short, single-thought replies built entirely from known words are
    read; one unknown word, a question mark, a negation, a relative or a
    different name returns None and the turn is judged semantically.
    """
    raw = (text or "").strip()
    if not raw or "?" in raw or "？" in raw or OTHER_PERSON.search(raw):
        return None
    tokens = _tokens(raw)
    if not tokens or len(tokens) > _MAX_REPLY_WORDS:
        return None
    if any(
        t in _NEGATIONS or t in _QUESTION_WORDS or t in _RELATION_WORDS
        for t in tokens
    ):
        return None
    subject_keys = subject_name_keys(subject_name)
    lead = 0
    while lead < len(tokens) and tokens[lead] in _AFFIRM_WORDS:
        lead += 1
    rest = tokens[lead:]
    identified = _self_identified(rest, subject_keys, lead > 0) if rest else None
    if identified is None and rest:
        # "गौरव बात कर रहा हूँ, आगे बोलो": a self-identification followed by
        # a go-ahead still states who is speaking.
        trimmed = list(rest)
        while trimmed and (
            trimmed[-1] in _GO_AHEAD_WORDS or trimmed[-1] in _COURTESY_WORDS
            or trimmed[-1] in _SOFTENERS
        ):
            trimmed.pop()
        if trimmed and len(trimmed) < len(rest):
            identified = _self_identified(trimmed, subject_keys, lead > 0)
    if identified is not None:
        return SELF_IDENTIFIED if identified else None
    if lead == 0:
        return None
    go_ahead = False
    for index, token in enumerate(rest):
        if token in _GO_AHEAD_WORDS:
            go_ahead = True
        elif token in _SOFTENERS and index and rest[index - 1] in _GO_AHEAD_WORDS:
            continue
        elif token not in _COURTESY_WORDS and token not in _AFFIRM_WORDS:
            return None
    return GO_AHEAD if go_ahead else AFFIRM


def mentions_go_ahead(text: str) -> bool:
    """Whether a reply invites the bot to go on ("boliye", "bataiye", …)."""
    return any(t in _GO_AHEAD_WORDS for t in _tokens(text))


def identity_reply_outcome(
    kind: str | None, *, mode: str, explicit_asked: bool
) -> str | None:
    """``confirm``, ``explicit_reask`` or None (leave it to the decision)."""
    if kind in (AFFIRM, SELF_IDENTIFIED):
        return CONFIRM
    if kind == GO_AHEAD:
        if mode == STRICT and not explicit_asked:
            return EXPLICIT_REASK
        return CONFIRM
    return None


# ── re-ask ladder ────────────────────────────────────────────────────────────
# Every rung asks explicitly whether the caller IS the person, each in its own
# words; rung 0 is the strict mode's explicit question. The bot never speaks
# the same identity question a third time: the ladder never repeats a rung,
# and a rung that coincides with the greeting is spoken at most once more.
# Hindi wording avoids first-person verbs, so it fits either voice gender.
_REASKS: dict[str, dict[str, str]] = {
    "identity_reask_0": {
        "en": "Is this {name} speaking?",
        "hi": "क्या आप ही {name} जी बोल रहे हैं?",
        "ml": "{name} തന്നെയാണോ സംസാരിക്കുന്നത്?",
        "ta": "{name} தானா பேசுவது?",
    },
    "identity_reask_1": {
        "en": "Sorry, could you just confirm — are you {name}?",
        "hi": "माफ़ कीजिए, बस इतना confirm कर दीजिए — क्या आप {name} जी हैं?",
        "ml": "ക്ഷമിക്കണം, ഒന്ന് ഉറപ്പാക്കട്ടെ — നിങ്ങൾ {name} ആണോ?",
        "ta": "மன்னிக்கவும், ஒன்று உறுதிப்படுத்துங்கள் — நீங்கள் தானா {name}?",
    },
    "identity_reask_2": {
        "en": "Please answer yes or no — am I speaking with {name}?",
        "hi": "कृपया हाँ या ना में बताइए — आप {name} जी ही हैं न?",
        "ml": "അതെ അല്ലെങ്കിൽ ഇല്ല എന്ന് പറയൂ — {name} തന്നെയാണോ?",
        "ta": "ஆம் அல்லது இல்லை என்று சொல்லுங்கள் — நான் {name} உடன் தான் பேசுகிறேனா?",
    },
    # No name on record: the same three asks about "the person this call is
    # for".
    "identity_reask_0_unnamed": {
        "en": "Sorry — am I speaking with the person this call is for?",
        "hi": "माफ़ कीजिए, क्या आप ही वही व्यक्ति बोल रहे हैं जिनके लिए यह call है?",
        "ml": "ക്ഷമിക്കണം, ഈ കോൾ ആർക്കാണോ അവർ തന്നെയാണോ സംസാരിക്കുന്നത്?",
        "ta": "மன்னிக்கவும், இந்த அழைப்பு யாருக்கோ அவரிடம் தான் பேசுகிறேனா?",
    },
    "identity_reask_1_unnamed": {
        "en": "Could you just confirm that this call is meant for you?",
        "hi": "बस इतना confirm कर दीजिए — क्या यह call आपके लिए ही है?",
        "ml": "ഈ കോൾ നിങ്ങൾക്കുള്ളതാണോ എന്ന് ഒന്ന് ഉറപ്പാക്കാമോ?",
        "ta": "இந்த அழைப்பு உங்களுக்கானதா என்று உறுதிப்படுத்த முடியுமா?",
    },
    "identity_reask_2_unnamed": {
        "en": "Please answer yes or no — is this call for you?",
        "hi": "कृपया हाँ या ना में बताइए — क्या मेरी बात सही व्यक्ति से हो रही है?",
        "ml": "അതെ അല്ലെങ്കിൽ ഇല്ല എന്ന് പറയൂ — ഈ കോൾ നിങ്ങൾക്കുള്ളതാണോ?",
        "ta": "ஆம் அல்லது இல்லை என்று சொல்லுங்கள் — இந்த அழைப்பு உங்களுக்குத் தானா?",
    },
}
REASK_RUNGS = 3
_REASK_LANGUAGES = frozenset({"en", "hi", "ml", "ta"})


def identity_reask(rung: int, locale: str | None, name: str = "") -> str:
    """Re-ask ``rung`` (0-based, clamped to the ladder) in the call language
    (English for languages the ladder does not carry)."""
    rung = max(0, min(int(rung), REASK_RUNGS - 1))
    name = (name or "").strip()
    key = f"identity_reask_{rung}" + ("" if name else "_unnamed")
    return resolve_phrase(_REASKS, key, locale).format(name=name)


def reask_has_language(locale: str | None) -> bool:
    return (locale or "en").split("-")[0].lower() in _REASK_LANGUAGES


def identity_question_sentences(text: str) -> list[str]:
    """The identity-question sentences of one bot utterance."""
    return [
        s.strip() for s in _SENTENCES.split(text or "")
        if s.strip() and IDENTITY_QUESTION.search(s)
    ]


def _last_question(text: str) -> str:
    questions = [
        s.strip() for s in _SENTENCES.split(text or "")
        if s.strip().endswith(("?", "？"))
    ]
    return questions[-1] if questions else ""


class IdentityConfirmation:
    """One call's identity-question state for the brain.

    Generic bots (no domain policy) take their pending/confirmed state from
    here; collections calls keep theirs in the policy and use this only for
    the wording count. ``pending`` is read off the bot's last utterance, so a
    rewound (never heard) reply is handled by the brain's own bookkeeping.
    """

    def __init__(self, *, mode: str, subject_name: str = "") -> None:
        self.mode = mode
        self.subject_name = (subject_name or "").strip()
        self.confirmed = False
        self.denied = False
        self.confirmed_turn = 0
        # The strict mode's explicit question was asked (any ladder rung).
        self.explicit_asked = False
        self.reasks = 0
        self._asked: Counter[tuple[str, ...]] = Counter()
        self._ladder: set[str] = set()
        self._subject_keys = subject_name_keys(self.subject_name)

    # ── what the bot said ─────────────────────────────────────────────────

    def _is_identity_sentence(self, sentence: str) -> bool:
        """An identity question about THIS call's subject: it names them when
        a name is known ("क्या मेरी बात gaurav जी से हो रही है?"); without a
        name the broad English forms alone do not count."""
        sentence = sentence.strip()
        if sentence in self._ladder:
            return True
        if not IDENTITY_QUESTION.search(sentence):
            return False
        if self._subject_keys:
            return any(
                _name_key(word) in self._subject_keys
                for word in normalize_words(sentence)
            )
        return bool(IDENTITY_QUESTION.search(_BROAD_IDENTITY.sub(" ", sentence)))

    def is_identity_ask(self, text: str) -> bool:
        """Whether ``text`` ends on the identity question (or is a rung)."""
        question = _last_question(text)
        return bool(question) and self._is_identity_sentence(question)

    def pending(self, last_bot_reply: str) -> bool:
        return (
            not self.confirmed and not self.denied
            and self.is_identity_ask(last_bot_reply)
        )

    def _wording(self, sentence: str) -> tuple[str, ...]:
        words = []
        for word in normalize_words(sentence):
            key = _name_key(word)
            if key in self._subject_keys or word == "ji":
                continue
            words.append(key)
        return tuple(words)

    def _ask_sentences(self, text: str) -> list[str]:
        return [
            s.strip() for s in _SENTENCES.split(text or "")
            if s.strip() and self._is_identity_sentence(s)
        ]

    def observe_bot(self, text: str) -> None:
        """Count every identity question the bot just spoke, by wording."""
        for sentence in self._ask_sentences(text):
            self._asked[self._wording(sentence)] += 1

    def times_asked(self, sentence: str) -> int:
        return self._asked[self._wording(sentence)]

    def repeated_wordings(self) -> list[tuple[str, ...]]:
        return [w for w, n in self._asked.items() if n >= 2]

    # ── what the caller said ──────────────────────────────────────────────

    def rule(self, text: str) -> str | None:
        """The deterministic outcome for a reply to the pending question."""
        kind = classify_identity_reply(text, self.subject_name)
        return identity_reply_outcome(
            kind, mode=self.mode, explicit_asked=self.explicit_asked,
        )

    def confirm(self, turn: int) -> None:
        self.confirmed = True
        self.confirmed_turn = turn

    # ── re-asks ───────────────────────────────────────────────────────────

    def next_reask(self, locale: str | None) -> str:
        """The next ladder rung whose wording was not already spoken twice,
        or "" once the ladder is used up."""
        while self.reasks < REASK_RUNGS:
            text = identity_reask(self.reasks, locale, self.subject_name)
            self.reasks += 1
            if self.times_asked(text) < 2:
                self.explicit_asked = True
                self._ladder.add(text)
                return text
        return ""

    def rephrase_repeated(self, text: str, locale: str | None) -> str:
        """``text`` with an identity question already spoken twice in the
        same words swapped for the next ladder rung ("" when nothing to do)."""
        for sentence in self._ask_sentences(text):
            if self.times_asked(sentence) >= 2:
                rung = self.next_reask(locale)
                if not rung:
                    return ""
                return text.replace(sentence, rung, 1)
        return ""


__all__ = [
    "AFFIRM", "CONFIRM", "EXPLICIT_REASK", "GO_AHEAD", "IDENTITY_QUESTION",
    "OTHER_PERSON", "REASK_RUNGS", "SELF_IDENTIFIED", "IdentityConfirmation",
    "classify_identity_reply", "identity_question_sentences", "identity_reask",
    "identity_reply_outcome", "mentions_go_ahead", "reask_has_language",
    "subject_name_keys",
]
