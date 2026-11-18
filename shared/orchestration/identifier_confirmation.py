"""Caller confirmation of collected identifiers, shared by every workflow.

Candidates stay in private checkpoint state until the caller confirms them.
They are never included in exported slots, audit entries or tool arguments.
"""
from __future__ import annotations

import re

from shared.orchestration.router import looks_like_question

_REFERENCE_WORDS = {"id", "identifier", "reference", "ref", "serial", "tid", "mid"}
_PHONE_WORDS = {"phone", "mobile", "telephone"}
_SECRET_WORDS = {"otp", "password", "passcode", "pin", "cvv", "cvc"}
_NON_REFERENCE_TYPES = {"date", "date_range", "time", "duration", "currency",
                        "percentage", "spoken_day", "spoken_time", "boolean"}
_MEASUREMENT_WORDS = {"amount", "price", "cost", "quantity", "count", "age", "floor",
                      "guests", "people", "weight", "height", "distance", "duration"}
_IDENTIFIER_TYPES = {"identifier", "reference", "account_number", "policy_number", "claim_number"}
_LETTERS = dict(zip(
    ("ए", "बी", "सी", "डी", "ई", "एफ", "जी", "एच", "आई", "जे", "के", "एल",
     "एम", "एन", "ओ", "पी", "क्यू", "आर", "एस", "टी", "यू", "वी", "डब्ल्यू", "एक्स", "वाई", "ज़ेड"),
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
))
_LETTERS.update({"जेड": "Z", "ज़ेड": "Z"})
# Python's \w excludes Indic vowel marks: a bare \b would recognize the ए
# inside बोलिए as the letter A. Treat marks and joiners as part of a word.
_WORD_CHAR = r"[\w\u0300-\u036f\u0900-\u0963\u0971-\u097f\u1cd0-\u1cff\u200c\u200d]"
_TOKEN_START = r"(?<!" + _WORD_CHAR + r")"
_TOKEN_END = r"(?!" + _WORD_CHAR + r")"
# A spelling word must corroborate the stated letter. Unknown/mismatched
# phonetics stay ambiguous; they never turn into a guessed suffix.
_HINDI_PHONETICS = {
    "अल्फा": "A", "ऐल्फा": "A", "एप्पल": "A", "ऐपल": "A", "एपल": "A",
    "बॉय": "B", "बाय": "B", "बॉल": "B", "ब्रावो": "B",
    "चार्ली": "C", "कैट": "C", "डेल्टा": "D", "डॉग": "D",
    "इको": "E", "ईको": "E", "एको": "E", "एलिफेंट": "E",
    "फॉक्सट्रॉट": "F", "फिश": "F", "गोल्फ": "G", "गोट": "G",
    "होटल": "H", "हाउस": "H", "इंडिया": "I", "जूलियट": "J", "जोकर": "J",
    "किलो": "K", "काइट": "K", "लीमा": "L", "लायन": "L",
    "माइक": "M", "मैंगो": "M", "मेंगो": "M", "मंगो": "M",
    "नवंबर": "N", "नेस्ट": "N", "ऑस्कर": "O", "ऑरेंज": "O",
    "पापा": "P", "पैरट": "P", "क्यूबेक": "Q", "क्वीन": "Q",
    "रोमियो": "R", "रैबिट": "R", "सिएरा": "S", "सन": "S",
    "टैंगो": "T", "टाइगर": "T", "यूनिफॉर्म": "U", "अंब्रेला": "U",
    "विक्टर": "V", "वैन": "V", "व्हिस्की": "W", "वॉच": "W",
    "एक्सरे": "X", "यांकी": "Y", "याक": "Y", "ज़ूलू": "Z", "जीब्रा": "Z",
}
_LETTER_TOKEN = r"(?:" + "|".join(map(re.escape, sorted(_LETTERS, key=len, reverse=True))) + r"|[A-Za-z])"
# जी names the letter G only when it is being spelled. Beside an
# acknowledgement ("हाँ जी", "जी हाँ", "जी सर") or opening the utterance before
# a pause it is the Hindi honorific: "हां जी। 1964939 A for Alpha" was read
# back as "letter G, one, nine, …" and sent to the lookup as G1964939A (DEMO
# cv_8358a2aef66b, 2026-11-17). A spelled "जी फॉर गोल्फ" is resolved earlier.
_ACKNOWLEDGEMENT = r"(?:हाँ|हां|हा|जी|yes|yeah|haan|han|ha|ok|okay|ओके|अच्छा|ठीक\s+है)"
_JI_AFTER_ACKNOWLEDGEMENT = re.compile(
    _TOKEN_START + r"(" + _ACKNOWLEDGEMENT + r")[\s,]+जी" + _TOKEN_END, re.I,
)
_JI_BEFORE_ACKNOWLEDGEMENT = re.compile(
    _TOKEN_START + r"जी(?=[\s,]+(?:हाँ|हां|हा|नहीं|नही|सर|मैडम|sir|madam)" + _TOKEN_END + r")", re.I,
)
_JI_OPENING = re.compile(r"^\s*जी(?=\s*(?:[,.!।॥]|$))")
_PHONETIC = re.compile(
    _TOKEN_START + r"(?:(?P<repeat>double|triple|डबल|ट्रिपल)\s+)?"
    r"(?:(?P<echo>" + _LETTER_TOKEN + r")[\s,.;।-]+)?"
    r"(?P<letter>" + _LETTER_TOKEN + r"|[A-Za-z]{2})\s+"
    r"(?:for|as\s+in|फोर|फ़ोर|फॉर|फ़ॉर)\s+"
    # Danda/double danda are sentence punctuation, not part of the spelling
    # word. Including them made the exact spoken `अल्फा।` fail corroboration.
    r"(?P<word>[A-Za-z]+|[\u0900-\u0963\u0971-\u097F]+)" + _TOKEN_END, re.I,
)
# "A for Alpha" is regularly transcribed "Eight for Alpha": the letter A and
# the digit eight sound alike on a phone line. The readback then carried an
# extra 8 and no letter, the caller confirmed it and the lookup failed (DEMO
# cv_a3a2e9c1d463: "301134 Six, Eight for Alpha." → 30113468). Nobody spells
# a digit with a word, so a spelling word that starts with A decides.
_EIGHT_FOR_A = re.compile(
    _TOKEN_START + r"(?:eight|8|एट|ऐट)(?P<rest>\s+(?:for|as\s+in|फोर|फ़ोर|फॉर|फ़ॉर)\s+"
    r"(?P<word>[A-Za-z]+|[\u0900-\u0963\u0971-\u097F]+))" + _TOKEN_END, re.I,
)
_REPEATED_LETTER = re.compile(
    _TOKEN_START + r"(double|triple|डबल|ट्रिपल)\s+(" + _LETTER_TOKEN + r")" + _TOKEN_END, re.I,
)
_CONTINUATION = re.compile(r"^\s*(?:and|aur|और)\b[\s,.;।-]*", re.I)
# "The letter comes at the end": the caller corrects a readback that lost its
# trailing letter without dictating the whole identifier again. "लास्ट में ए
# है", "आपने लास्ट में ए नहीं बोला" and "उसके बाद। A for Apple" were each
# answered with the same re-prompt until the gate gave up and the call was
# closed (DEMO cv_cd292d6c02cd, cv_a9af6b51d328, 2026-11-18).
_SUFFIX_POSITION = (
    r"(?:(?:last|end|ending|aakhir|aakhiri|aakhri|akhir|ant|लास्ट|एंड|आखिर|आख़िर|आखिरी|आख़िरी|अंत)"
    r"(?:\s+(?:में|मे|mein|me|main|par|pe|पर|पे))?"
    r"|(?:uske|iske|उसके|इसके)\s+(?:baad|बाद)|after\s+that|at\s+the\s+end)"
)
_SUFFIX_LEAD = r"(?:(?:no|nahi|nahin|नहीं|नही|haan|हाँ|हां|ji|जी|sir|सर|aapne|आपने|aur|और|and|but|lekin|लेकिन)[\s,]+)*"
_SUFFIX_TAIL = (
    r"(?:\s+(?:भी|bhi))?(?:\s+(?:है|हैं|hai|hain|आता\s+है|aata\s+hai|लगेगा|lagega|"
    r"(?:नहीं|नही|nahi|nahin)\s+(?:बोला|बोले|कहा|bola|bole|kaha)|छूट\s+गया|रह\s+गया))?"
)
_SUFFIX_STATEMENT = re.compile(
    r"^" + _SUFFIX_LEAD + _SUFFIX_POSITION + r"[\s,]+(?P<letter>.+?)" + _SUFFIX_TAIL + r"$", re.I,
)
_SUFFIX_POSITION_ONLY = re.compile(r"^" + _SUFFIX_LEAD + _SUFFIX_POSITION + r"$", re.I)
_SUFFIX_ACK = re.compile(r"^(?:(?:no|nahi|nahin|नहीं|नही|haan|हाँ|हां|ji|जी|sir|सर)[\s,]*)+$", re.I)
_REPETITION_PREFIX = re.compile(
    r"^\s*(?:i\s+(?:just\s+)?said|"
    r"maine\s+(?:abhi\s+)?(?:bola|kaha|bataya)(?:\s+(?:na|tha))?|"
    r"मैंने\s+(?:अभी\s+)?(?:बोला|कहा|बताया)(?:\s+(?:ना|था))?)"
    r"[\s,:;।-]+", re.I,
)
_RESTART_PREFIX = re.compile(
    r"^\s*(?:start\s+again|again\s+from\s+(?:the\s+)?(?:start|beginning)|"
    r"phir\s+se|dobara|फिर\s+से|दोबारा|wrong|incorrect|galat|गलत|ग़लत)"
    r"[\s,:;।—-]+", re.I,
)
_ID_LABEL = (
    r"(?:(?:terminal|booking|order|complaint|ticket|reference|account|policy|claim|"
    r"टर्मिनल|बुकिंग|ऑर्डर|शिकायत)\s+)?"
    r"(?:id|tid|mid|identifier|number|no|आईडी|आई\s*(?:डी|टी)|नंबर|नम्बर)|"
    r"(?:phone|mobile|फोन|फ़ोन|मोबाइल)(?:\s+(?:number|नंबर|नम्बर))?"
)
_IDENTIFIER_PREFIX = re.compile(
    r"^\s*(?:(?:my|mera|meri|मेरा|मेरी)\s+)?(?:" + _ID_LABEL + r")"
    r"(?:\s+(?:" + _ID_LABEL + r"))?\s+(?:is|hai|hain|है|हैं)[\s,:;।-]+", re.I,
)
# Unlike digits_dominant's tolerant prose heuristic, buffered IDs require
# every remaining token to be dictation. Strip only known hesitation/control
# fillers; arbitrary English or Hindi words must remain visible to the gate.
_DICTATION_FILLER = re.compile(
    _TOKEN_START + r"(?:uh|um|umm|hmm|hm|ah|aa|haan|han|ji|yes|ok|okay|so|"
    r"it's|its|is|the|अच्छा|हाँ|हां|तो)" + _TOKEN_END
    + r"(?!\s+(?:for|as\s+in|फोर|फ़ोर|फॉर|फ़ॉर)" + _TOKEN_END + r")", re.I,
)
_DICTATION_CONNECTOR = re.compile(
    r"(?<=[A-Za-z0-9])[\s,.;।-]+(?:and|aur|और)[\s,.;।-]+(?=[A-Za-z0-9])", re.I,
)
_COURTESY = r"(?:please|sir|madam|कृपया|प्लीज|सर|मैडम)"
_DICTATION_COURTESY_START = re.compile(r"^\s*(?:" + _COURTESY + r"[\s,.!;।-]+){1,2}", re.I)
_DICTATION_COURTESY_END = re.compile(r"(?:[\s,.!;।-]+" + _COURTESY + r"){1,2}[\s,.!;।-]*$", re.I)
_NEGATIVE = re.compile(r"(?<![\w./_-])(?:no|not|wrong|incorrect|nahi|nahin|nhi|naahi|galat|नहीं|नही|नाही|गलत|ग़लत|இல்லை|അല്ല)(?![\w/_-])", re.I)
_CORRECTION = re.compile(r"(?<!\w)(?:but|actually|instead|except|ending|last|first|replace|correction|lekin|लेकिन|आखिरी|आख़िरी|बदलो|बदल|सुधार)(?!\w)", re.I)
# Natural whole-utterance confirmations. Every token must be on this list and
# at least one must be a real affirmative (_AFFIRMATIVE), so "it is" or
# "okay" alone never confirms. "हाँ, यह सही है।", "बरोबर." and "ठीक है" were
# rejected at the Terminal ID readback although the classifier labelled them
# affirm 0.99 (DEMO cv_c337303f00bb / cv_40ec07f737d3, 2026-11-17).
_YES = re.compile(
    r"^(?:(?:yes|yeah|yep|correct|confirmed|right|absolutely|perfect|exactly|haan|han|haa|ha|haanji|hanji|ji|"
    r"sahi|hai|hain|bilkul|ekdum|ekdam|theek|thik|barobar|barabar|yahi|yehi|wahi|vahi|yah|yeh|ye|hi|to|"
    r"aapne|kaha|bola|bataya|sir|madam|mam|ma'am|bhai|okay|ok|it|is|it's|that's|that|this|"
    r"हाँ|हां|हा|हाँजी|हांजी|जी|सही|है|हैं|बिल्कुल|बिलकुल|एकदम|ठीक|बरोबर|बराबर|करेक्ट|राइट|यस|ओके|"
    r"यह|ये|यही|वही|ही|तो|आपने|कहा|बोला|बताया|सर|मैडम|भाई|ஆமாம்|சரி|അതെ|ശരി)[\s,.!।]*)+$", re.I,
)
_AFFIRMATIVE = re.compile(
    r"(?<!\w)(?:yes|yeah|yep|correct|confirmed|right|absolutely|perfect|exactly|haan|han|haa|ha|haanji|hanji|ji|"
    r"sahi|bilkul|theek|thik|barobar|barabar|yahi|yehi|wahi|vahi|"
    r"हाँ|हां|हा|हाँजी|हांजी|जी|सही|बिल्कुल|बिलकुल|ठीक|बरोबर|बराबर|करेक्ट|राइट|यस|यही|वही|"
    r"ஆமாம்|சரி|അതെ|ശരി)(?!\w)", re.I,
)
# A negation always wins over the natural affirmatives accepted by _YES:
# "theek nahi hai", "sahi nahi hai", "barobar nahi", "yah sahi nahi hai" are
# a clear no (they used to be neither answer, so the gate only repeated its
# prompt). Any word outside this list keeps the reply undecided.
_NO = re.compile(
    r"^(?:(?:no|not|wrong|incorrect|isn't|isnt|nahi|nahin|nhi|naahi|galat|"
    r"yes|yeah|haan|han|haa|ha|haanji|hanji|ji|okay|ok|it|is|it's|that|that's|this|correct|confirmed|right|"
    r"perfect|exactly|absolutely|hai|hain|sir|madam|mam|bhai|id|tid|"
    r"sahi|theek|thik|barobar|barabar|bilkul|ekdum|ekdam|yah|yeh|ye|yahi|yehi|"
    r"नहीं|नही|नाही|गलत|ग़लत|हाँ|हां|हा|हाँजी|हांजी|जी|यह|ये|यही|सही|ठीक|बरोबर|बराबर|बिल्कुल|बिलकुल|एकदम|"
    r"है|हैं|सर|मैडम|भाई|आईडी|இல்லை|അല്ല)[\s,.!।]*)+$", re.I,
)


def _field_words(value: str) -> set[str]:
    # Both bookingId and complaintID are common in existing tenant schemas.
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", str(value))
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return set(re.findall(r"[a-z]+|[0-9]+", value.lower()))


def identifier_kind(node: dict, variable: str) -> str | None:
    """Schema semantics first, generic field-name compatibility second.

    No tenant, bot, or business-specific ID name is needed. Opaque custom
    fields opt in with dataType=identifier or requireConfirmation=true.
    The default false on older entities never disables the common ID rule.
    """
    config = node.get("config") if isinstance(node.get("config"), dict) else {}
    entity = config.get("entity") if isinstance(config.get("entity"), dict) else {}
    patterns = entity.get("synonymPatterns") or entity.get("synonym_patterns")
    names = [_field_words(variable), _field_words(entity.get("name") or "")]
    words = set().union(*names)
    dtype = str(entity.get("dataType") or entity.get("data_type")
                or config.get("entityType") or config.get("dataType") or "text").lower()
    semantics = str(entity.get("semanticType") or config.get("semanticType") or "").lower()
    if (config.get("prefillOnly") or config.get("confirmsIdentifier")
            or words & _SECRET_WORDS or _field_words(dtype) & _SECRET_WORDS
            or semantics in {"secret", "authentication"}
            or any({"card", "number"} <= name for name in names)
            or (isinstance(patterns, dict) and {str(k).lower() for k in patterns} == {"yes", "no"})):
        return None
    from shared.orchestration.ask_resolution import _yes_no_canonicals
    if (_yes_no_canonicals(entity) is not None
            or {str(v).lower() for v in entity.get("allowedValues") or []} == {"yes", "no"}):
        return None
    if dtype == "phone" or semantics == "phone":
        return "phone"
    if (dtype in _IDENTIFIER_TYPES or semantics in {"identifier", "reference"}
            or entity.get("requireConfirmation") is True or config.get("requireConfirmation") is True):
        return "identifier"
    if dtype in _NON_REFERENCE_TYPES:
        return None
    if words & _PHONE_WORDS or {"contact", "number"} <= words:
        return "phone"
    if words & _MEASUREMENT_WORDS:
        return None
    if words & _REFERENCE_WORDS:
        return "identifier"
    # ticketNo, complaint_number, shipment-number, etc. share a reference
    # shape; amounts and measurements remain ordinary numeric answers.
    if (words & {"number", "no"} and len(words) > 1
            and any(re.search(r"(?:number|no)\d*$", re.sub(r"[^A-Za-z0-9]", "", name), re.I)
                    for name in (variable, str(entity.get("name") or "")))):
        return "identifier"
    return None


def _letter(value: str) -> str | None:
    value = _LETTERS.get(value, value.upper())
    return value[0] if value and len(set(value)) == 1 and value[0] in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" else None


def _eight_as_letter_a(match: re.Match) -> str:
    word = match["word"]
    names_a = word[0].upper() == "A" if word.isascii() else _HINDI_PHONETICS.get(word) == "A"
    return "A" + match["rest"] if names_a else match[0]


def _identifier_spelling(text: str, *, mark_phonetics: bool = False) -> tuple[str, bool]:
    """Resolve spelling *before* number words (Hindi `फोर` also means four)."""
    valid = True

    def replace(match: re.Match) -> str:
        nonlocal valid
        letter = _letter(match["letter"])
        word = match["word"]
        phonetic = word[0].upper() if word.isascii() else _HINDI_PHONETICS.get(word)
        echo = _letter(match["echo"]) if match["echo"] else letter
        if not letter or phonetic != letter or echo != letter:
            valid = False
            return match[0]
        # B, B for boy / MM for mango are corroborated spelling echoes.
        # An explicit double/triple remains multiple letters.
        count = 3 if (match["repeat"] or "").lower() in {"triple", "ट्रिपल"} else 2 if match["repeat"] else 1
        result = " ".join([letter] * count)
        return "\ue000" + result + "\ue001" if mark_phonetics else result

    value = _EIGHT_FOR_A.sub(_eight_as_letter_a, str(text or ""))
    value = _PHONETIC.sub(replace, value)
    value = _REPEATED_LETTER.sub(
        lambda m: " ".join([_letter(m[2])] * (3 if m[1].lower() in {"triple", "ट्रिपल"} else 2)), value,
    )
    return value, valid


def identifier_spelling_valid(text: str) -> bool:
    """A stated letter and its spelling word must agree before extraction."""
    return _identifier_spelling(text)[1]


def _without_honorific_ji(value: str) -> str:
    """Drop जी where it is an honorific, so it is not read as the letter G."""
    previous = None
    while previous != value:  # "हाँ जी जी" needs a second pass
        previous = value
        value = _JI_AFTER_ACKNOWLEDGEMENT.sub(lambda m: m[1], value)
    value = _JI_BEFORE_ACKNOWLEDGEMENT.sub("", value)
    return _JI_OPENING.sub("", value)


def spoken_identifier_text(text: str) -> str:
    """Normalize corroborated dictation without sound-alike substitutions."""
    from shared.orchestration.spoken_numbers import spoken_digit_text

    value, _ = _identifier_spelling(text, mark_phonetics=True)
    value = _without_honorific_ji(value)
    value = spoken_digit_text(value)
    for word, letter in _LETTERS.items():
        value = re.sub(_TOKEN_START + re.escape(word) + _TOKEN_END, letter, value)
    # A connector belongs to the ID only between a numeric run and a
    # following isolated letter. Other prose remains untouched.
    value = re.sub(r"(?<=\d)[\s,.;।-]+(?:and|aur|और)[\s,.;।-]+(?=\ue000)", " ", value, flags=re.I)
    value = value.replace("\ue000", "").replace("\ue001", "")
    value = re.sub(r"(?<=\d)[\s,.;।-]+(?:and|aur|और)[\s,.;।-]+(?=[A-Za-z](?:[\s,.;।-]+[A-Za-z])*[\s,.!;।-]*$)", " ", value, flags=re.I)
    # Join only adjacent single letters / digit runs, leaving prose alone.
    token = r"(?:\d+|[A-Za-z])"
    pattern = re.compile(_TOKEN_START + r"(" + token + r"(?:[\s,.;।-]+" + token + r")+)" + _TOKEN_END)
    return pattern.sub(lambda m: re.sub(r"[\s,.;।-]+", "", m[1]).upper(), value)


def confirmation_answer(text: str) -> str | None:
    """Constrained conversational yes/no; a question or correction is neither."""
    control = (text or "").replace("’", "'").strip(" \t\r\n,.!।")
    if (not control or looks_like_question(control) or _CORRECTION.search(control)
            or re.match(r"^isn'?t\s+(?:it|that|this)\b", control, re.I)):
        return None
    if _NEGATIVE.search(control) or re.search(r"\bisn'?t\b", control, re.I):
        return "no" if _NO.fullmatch(control) else None
    return "yes" if _YES.fullmatch(control) and _AFFIRMATIVE.search(control) else None


def identifier_suffix(text: str) -> str | None:
    """One clear letter continuation, never arbitrary prose or double letters."""
    if (looks_like_question(text or "") or confirmation_answer(text) is not None
            or not identifier_spelling_valid(text)):
        return None
    return _plain_suffix(text) or _positional_suffix(text)


def _plain_suffix(text: str) -> str | None:
    value = _REPETITION_PREFIX.sub("", (text or "").strip(" \t\r\n,.!।॥"))
    value = _CONTINUATION.sub("", value)
    value = re.sub(r"^(?:letter|लेटर)\s+", "", value, flags=re.I)
    value = spoken_identifier_text(value).strip(" \t\r\n,.!।")
    return value.upper() if re.fullmatch(r"[A-Za-z]", value) else None


def _positional_suffix(text: str) -> str | None:
    """"लास्ट में ए है" / "at the end A for Apple": one letter, one position.

    Every clause must be an acknowledgement, a bare position phrase or the
    same single letter; anything else (digits, other prose, two different
    letters) is not a suffix statement.
    """
    letters: set[str] = set()
    positioned = False
    for clause in (part.strip(" \t\r\n,") for part in re.split(r"[.!।॥]+", text or "")):
        if not clause or _SUFFIX_ACK.fullmatch(clause):
            continue
        if _SUFFIX_POSITION_ONLY.fullmatch(clause):
            positioned = True
            continue
        statement = _SUFFIX_STATEMENT.fullmatch(clause)
        letter = _plain_suffix(statement["letter"] if statement else clause)
        if not letter:
            return None
        positioned = positioned or bool(statement)
        letters.add(letter)
    return letters.pop() if positioned and len(letters) == 1 else None


def identifier_suffix_repeated(text: str) -> bool:
    """Explicitly repeating an earlier single letter, rather than adding one."""
    return bool(_REPETITION_PREFIX.match(text or "") and identifier_suffix(text))


def _dictation_payload(text: str) -> str:
    value = _RESTART_PREFIX.sub("", text or "")
    value = _IDENTIFIER_PREFIX.sub("", value)
    value = _REPETITION_PREFIX.sub("", value)
    return _DICTATION_FILLER.sub("", value).strip(" \t\r\n,.!।॥")


def canonical_identifier(value: str) -> str:
    return re.sub(r"\s+", "", str(value)).upper()


def identifier_sequence(text: str) -> str:
    if not identifier_spelling_valid(text):
        return ""
    normalized = spoken_identifier_text(text)
    runs = re.findall(_TOKEN_START + r"[A-Za-z]*\d+[A-Za-z0-9]*" + _TOKEN_END, normalized)
    if runs:
        return "".join(runs).upper()
    letter = re.fullmatch(r"\s*(?:letter\s+)?([A-Za-z])[\s,.!।॥]*", normalized, re.I)
    if letter:
        return letter[1].upper()
    # Explicitly dictated repeats may continue a numeric partial. Ordinary
    # all-letter words do not become dictation merely because they are ASCII.
    payload = _dictation_payload(text)
    letters = spoken_identifier_text(payload)
    if re.fullmatch(r"[A-Za-z]", letters):
        return letters.upper()
    if (re.fullmatch(r"[A-Za-z]+", letters)
            and (_REPEATED_LETTER.fullmatch(payload) or _PHONETIC.fullmatch(payload)
                 or re.fullmatch(_LETTER_TOKEN + r"(?:[\s,.;।-]+" + _LETTER_TOKEN + r")+", payload, re.I))):
        return letters.upper()
    return ""


def identifier_dictation(text: str) -> bool:
    if looks_like_question(text or "") or confirmation_answer(text) is not None:
        return False
    payload = _dictation_payload(text)
    normalized = spoken_identifier_text(payload)
    # Resolve spelling before removing courtesy: Sir in "S for Sir" is
    # corroborating evidence, while a trailing "sir" after digits is prose.
    normalized = _DICTATION_COURTESY_START.sub("", normalized)
    normalized = _DICTATION_COURTESY_END.sub("", normalized)
    normalized = _DICTATION_CONNECTOR.sub("", normalized)
    sequence = identifier_sequence(payload)
    # Preserve Unicode letters when checking the remainder: deleting Hindi
    # prose made a readback question containing एक बार look like the ID 1.
    return bool(sequence) and re.sub(r"[\W_]+", "", normalized).upper() == sequence


def spoken_readback(value: str) -> str:
    from shared.orchestration.spoken_numbers import english_identifier_readback

    return english_identifier_readback(value)


def confirmation_prompt(value: str, kind: str, language: str) -> str:
    from shared.orchestration.phrases import canned
    label = "phone number" if kind == "phone" else "ID"
    return canned("wf_identifier_confirm", language).replace("{label}", label).replace("{value}", spoken_readback(value))


def recapture_prompt(kind: str, language: str) -> str:
    from shared.orchestration.phrases import canned
    return canned("wf_phone_recapture" if kind == "phone" else "wf_identifier_recapture", language)


def raw_candidate(node: dict, variable: str, text: str) -> str | None:
    from shared.orchestration.ask_resolution import _ask_entity
    from shared.orchestration.entity_extractor import extract_entity

    entity = {**_ask_entity(node, variable), "pii": False, "maskingEnabled": False, "masking_enabled": False}
    result = extract_entity(text, entity)
    value = result.get("value") if result.get("matched") else None
    if value is None:
        return None
    canonical = canonical_identifier(value)
    # Semantic outcomes such as no_tid are authored labels, not dictated
    # references. Literal/letter-spelled allowed IDs (including all-letter
    # IDs) still qualify. Workflows may also name unavailable values directly.
    config = node.get("config") or {}
    unavailable = entity.get("unavailableValues") or config.get("unavailableValues") or []
    if canonical in {canonical_identifier(v) for v in unavailable}:
        return None
    if (result.get("method") in {"lexicon", "lexicon_pattern"}
            and canonical not in canonical_identifier(spoken_identifier_text(text))):
        return None
    # Legacy numeric matchers may intentionally store only an ID's numeric
    # lookup key. Still read the caller's attached letters back faithfully.
    sequence = identifier_sequence(text)
    if (identifier_kind(node, variable) == "identifier" and not any(c.isalpha() for c in canonical)
            and any(c.isalpha() for c in sequence)
            and re.sub(r"\D", "", sequence) == re.sub(r"\D", "", canonical)):
        canonical = sequence
    return canonical or None


def amend_identifier_suffix(node: dict, variable: str, previous: str, text: str) -> str | None:
    """Append/replace one suffix only when the entire new ID validates.

    A single trailing letter can be replaced. Multiple existing letters or
    an all-letter reference do not reveal which part the caller meant.
    Phone numbers never accept alphabetic amendments.
    """
    from shared.orchestration.ask_resolution import _ask_entity
    from shared.orchestration.entity_extractor import extract_entity

    suffix = identifier_suffix(text)
    previous = canonical_identifier(previous)
    if (not suffix or identifier_kind(node, variable) != "identifier"
            or not any(c.isdigit() for c in previous)):
        return None
    ending = re.search(r"[A-Z]+$", previous)
    if ending and len(ending[0]) != 1:
        return None
    candidate = (previous[:-1] if ending else previous) + suffix
    entity = {**_ask_entity(node, variable), "pii": False, "maskingEnabled": False, "masking_enabled": False}
    extracted = extract_entity(candidate, entity)
    if not extracted.get("matched") or canonical_identifier(extracted.get("value") or "") != candidate:
        return None
    return candidate


def start_confirmation(ctx, *, offered: str | None = None) -> bool:
    """Intercept an otherwise valid capture before it becomes a slot."""
    kind = identifier_kind(ctx.node, ctx.variable)
    if not kind or ctx.identifier_confirmed or ctx.config.get("_externalIdentifierConfirmation"):
        return False
    source = offered if offered is not None else (ctx.combined if ctx.accumulated else ctx.text)
    raw = raw_candidate(ctx.node, ctx.variable, source)
    if raw is None:
        return False  # e.g. an explicitly authored `no_tid` outcome
    ctx.slots.pop(ctx.variable, None)
    ctx.pending_digits.pop(ctx.node_id, None)
    ctx.pending_confirmations[ctx.node_id] = {"variable": ctx.variable, "value": ctx.value,
                                             "raw": raw, "kind": kind, "attempts": 0}
    ctx.audit.append({"action": "identifier_confirmation_requested", "node": ctx.node_id,
                      "variable": ctx.variable})
    ctx.replies.append(confirmation_prompt(raw, kind, ctx.lang))
    ctx.stay()
    return True


def resolve_confirmation(ctx) -> bool:
    """Corrections and rejection always outrank an affirmation or LLM label."""
    pending = ctx.pending_confirmations.get(ctx.node_id)
    if not pending or pending.get("variable") != ctx.variable:
        return False
    # An all-letter ID matcher can also accept YES / NO. Confirmation words
    # retain their conversational meaning instead of replacing the ID.
    control = ctx.text.replace("’", "'").strip(" \t\r\n,.!।")
    answer = confirmation_answer(control)
    affirmative = answer == "yes"
    amended = amend_identifier_suffix(ctx.node, ctx.variable, pending["raw"], ctx.text)
    if amended is not None:
        from shared.orchestration.ask_resolution import _extract_ask_value
        ctx.value = _extract_ask_value(ctx.node, ctx.variable, amended)
        start_confirmation(ctx, offered=amended)
        return True
    candidate_source = ctx.text
    # With alphabetic references, a broad authored matcher can otherwise
    # capture "no" / "actually" instead of the replacement that follows.
    while True:
        candidate_source = candidate_source.lstrip(" \t\r\n,.!;:।")
        prefix = (_AFFIRMATIVE.match(candidate_source) or _NEGATIVE.match(candidate_source)
                  or _CORRECTION.match(candidate_source))
        if (not prefix or prefix.end() == len(candidate_source)
                or candidate_source[prefix.end()] not in " \t\r\n,!;:।"):
            break
        candidate_source = candidate_source[prefix.end():]
    candidate = (None if affirmative or answer == "no" or _NEGATIVE.fullmatch(control)
                 else raw_candidate(ctx.node, ctx.variable, candidate_source))
    prose = ctx.text
    if candidate:
        prose = re.sub(re.escape(candidate), "", prose, flags=re.I)
    negative = answer == "no" or bool(_NEGATIVE.search(prose))
    correction = bool(_CORRECTION.search(prose)) or bool(
        re.fullmatch(r"\s*(?:letter\s+)?[A-Za-z][\s,.!।]*", spoken_identifier_text(ctx.text), re.I))
    if candidate and candidate != pending["raw"]:
        from shared.orchestration.ask_resolution import _extract_ask_value
        ctx.value = _extract_ask_value(ctx.node, ctx.variable, candidate_source)
        start_confirmation(ctx, offered=candidate_source)
        return True
    if negative or correction:
        ctx.pending_confirmations.pop(ctx.node_id, None)
        ctx.pending_digits.pop(ctx.node_id, None)
        ctx.slots.pop(ctx.variable, None)
        ctx.audit.append({"action": "identifier_confirmation_rejected", "node": ctx.node_id,
                          "variable": ctx.variable})
        ctx.replies.append(recapture_prompt(pending["kind"], ctx.lang))
        ctx.stay()
        return True
    remainder = ctx.text.replace("’", "'")
    if candidate == pending["raw"]:
        remainder = re.sub(re.escape(candidate), "", spoken_identifier_text(remainder), flags=re.I)
    if (confirmation_answer(remainder) == "yes"
            and not looks_like_question(ctx.text)):
        ctx.value = pending["value"]
        ctx.identifier_confirmed = True
        ctx.pending_confirmations.pop(ctx.node_id, None)
        ctx.audit.append({"action": "identifier_confirmed", "node": ctx.node_id, "variable": ctx.variable})
        return False  # outcome_filled commits and advances once
    pending = {**pending, "attempts": pending.get("attempts", 0) + 1}
    ctx.pending_confirmations[ctx.node_id] = pending
    if pending["attempts"] > ctx.behavior.max_ask_retries:
        from shared.orchestration.phrases import canned
        ctx.pending_confirmations.pop(ctx.node_id, None)
        ctx.status = "handoff"
        ctx.current = ctx.awaiting = None
        ctx.replies.append(canned("wf_handover", ctx.lang))
    else:
        ctx.replies.append(confirmation_prompt(pending["raw"], pending["kind"], ctx.lang))
        ctx.stay()
    return True
