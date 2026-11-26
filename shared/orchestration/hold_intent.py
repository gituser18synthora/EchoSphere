"""Corroboration of a model ``hold`` label — is the caller asking us to wait?

The decision layer (Goal Engine / hybrid classifier) labels a turn ``hold``
from its MEANING.  The brain does not act on that label alone: an early model
called "Koi nahi hai sir" (an answer) and "haan bataiye kya bol rahe ho sir"
(a question right after a hold acknowledgement) holds, and honouring them
made the bot acknowledge and then wait in silence.  The first guard therefore
required the deterministic hold lexicon (``signals/core.py``: ruko, hold on,
wait, line par, "ek minute ruko" …) to agree.

That lexicon knows COMMANDS to wait.  Callers mostly ASK FOR TIME or say what
they are doing instead (DEMO 2026-11, 1,841 calls: 12 of 18 rejected labels
were genuine waits):

    "जी, मुझे थोड़ा टाइम दीजिए, मैं देख रहा हूँ।"      "हाँ, बताता हूँ एक मिनट।"
    "can you just give me some time so that I can check the VID?"
    "Thanks. Please stay on the line."             "हाय, एक मिनट, एक मिनट।"

Each of those was labelled ``hold`` at 0.98-0.99, rejected, and sent to the
workflow as an unmatched answer: "sorry, I did not understand", a burned
retry, and on the third one the ask's fallback edge.

This module is the second, wider corroboration tier — used ONLY to confirm a
label the model already issued, never as a signal of its own (so a regex
fallback turn, an edge label or a scripted route cannot become a hold through
it):

* :func:`hold_evidence` — wait wording the command lexicon does not cover: a
  request for time, a short duration, "stay on the line", what the caller is
  doing right now (looking / checking / searching / opening), "I'll tell you
  in a moment".  Hindi, Hinglish, English, plus the common Malayalam and
  Tamil forms.
* :func:`hold_counter_evidence` — why the turn is NOT a wait even with such
  wording: waiting is negated or impossible ("wait nahi kar sakta", "time
  nahi hai"), the caller is already back in the same breath ("एक मिनट यार। हो
  गया।", "… हाँ जी।", "… हलो।"), or the turn carries the answer itself (an
  identifier being dictated).

A label with evidence and no counter-evidence is honoured.  A label with
neither is honoured only for a tenant that opted in to trusting the decision
layer (``hold_trust_decision_enabled``) and only when it is not a question —
that is the path for phrasings and languages nobody listed.

One case has no label to corroborate: the model call failed or timed out
and the turn is read by regex alone (the hybrid classifier's 2.0 s budget;
LOCAL voice run 2026-11-26: "जी मुझे थोड़ा टाइम दीजिए मैं देख रहा हूँ" → timeout →
no signal → "sorry, I did not understand").  :func:`explicit_wait_request`
covers it with the two kinds of wording that ask the BOT for something — time,
or to stay on the line — and nothing softer; a "time" that is an appointment
("कल का टाइम दे दो", "शाम 5 बजे का time") is not a wait.
"""

from __future__ import annotations

import re

# A word character, Latin or Devanagari.  The danda "।" / "॥" (U+0964/65) sits
# inside the Devanagari block but ends a sentence, so it is left out.
_W = r"[\w\u0900-\u0963\u0966-\u097f]"
_B = r"(?<![\w\u0900-\u0963\u0966-\u097f])"     # start of a word
_E = r"(?![\w\u0900-\u0963\u0966-\u097f])"      # end of a word

# "a little / some / one / two" in front of time.
_SOME = (r"(?:thod[aie]|थोड़[ाीे]|थोड[ाीे]|zara|jara|ज़रा|जरा|kuch|कुछ|some|a\s+little|little|"
         r"a\s+bit(?:\s+of)?|more|aur|और|bas|बस|ek|एक|do|दो|one|two|a|an|couple\s+of|few|"
         r"a\s+few|paanch|panch|पाँच|पांच|das|दस|five|ten|\d{1,2})")
_TIME = r"(?:time|taa?im|टाइम|समय|samay|waqt|wakt|vakt|वक़्त|वक्त|मोहलत|mohlat)"
_UNIT = (r"(?:minutes?|mins?|mint|minat|mnt|seconds?|secs?|moments?|मिनट|मिनिट|मिंट|सेकंड|सेकेंड|"
         r"सेकण्ड|पल|क्षण)")
_GIVE = (r"(?:d[eo]|dijiye\w*|dijie\w*|deejiye|dena|dedo|de\s*do|de\s*dijiye|dijiyega|"
         r"दो|दे|दीजिए\w*|दीजिये\w*|देना|दे\s*दो|दे\s*दीजिए|दीजिएगा|लीजिए|लीजिये|lijiye)")
_NEED = r"(?:chahiye|chaiye|chahie|lagega|lagegi|lagenge|चाहिए|चाहिये|लगेगा|लगेगी|लगेंगे)"
# What the caller is doing while we wait.
_DOING = (r"(?:dekh|dhoo?n?dh|dhund|khoj|check\s*kar|nikaa?l|khol|search\s*kar|pata\s*kar|la|"
          r"likh|note\s*kar|"
          r"देख|ढूँढ|ढूंढ|ढूढ|खोज|चेक\s*कर|निकाल|खोल|सर्च\s*कर|पता\s*कर|ला|लिख|नोट\s*कर)")
_PROGRESSIVE = (r"(?:\s*(?:rah[aie]|रह[ाीे])\s*(?:hoon|hun|hu|hai|hain|हूँ|हूं|हु|है|हैं)"
                r"|\s*(?:t[aie]|त[ाीे])\s*(?:hoon|hun|hu|हूँ|हूं|हु)"
                r"|\s*(?:leta|leti|lete|deta|deti|लेता|लेती|लेते|देता|देती)\s*(?:hoon|hun|hu|हूँ|हूं|हु))")

_EVIDENCE: tuple[tuple[str, re.Pattern], ...] = tuple(
    (name, re.compile(pattern, re.I)) for name, pattern in (
        # "थोड़ा टाइम दीजिए", "ek minute dijiye", "give me a minute / some time".
        ("time_request",
         _B + r"(?:" + _SOME + r"\s+(?:sa\s+|si\s+|सा\s+|सी\s+)?)?(?:" + _TIME + r"|" + _UNIT + r")"
         r"\s+(?:to\s+|तो\s+|aur\s+|और\s+)?(?:" + _GIVE + r"|" + _NEED + r")" + _E
         + r"|\b(?:give|allow|grant)\s+me\s+(?:just\s+)?(?:" + _SOME + r"\s+)?(?:more\s+)?"
         r"(?:" + _TIME + r"|" + _UNIT + r")\b"
         r"|\bneed\s+(?:" + _SOME + r"\s+)?(?:more\s+)?(?:" + _TIME + r"|" + _UNIT + r")\b"
         r"|\btake\s+(?:" + _SOME + r"\s+)?(?:" + _TIME + r"|" + _UNIT + r")\b"),
        # "एक मिनट", "one second please", "bas do minute", "ഒരു മിനിറ്റ്".
        # Not "ek minute mein call cut ho gaya" / "दो मिनट में आ रहा है" /
        # "5 minutes ago": a duration that measures something else.
        ("duration",
         _B + _SOME + r"\s*(?:hi\s+|ही\s+)?" + _UNIT + _E
         + r"(?!\s*(?:mein|me|main|में|मे|pehle|pahle|पहले|ago|baad|बाद|se|से|tak|तक|ka|का|ke|के|ki|की)"
         + _E + r")"
         r"|\bjust\s+a\s+(?:sec|second|moment|minute|min)\b"
         r"|ഒരു\s*(?:മിനിറ്റ്|മിനിട്ട്|നിമിഷം|സെക്കൻഡ്|സെക്കന്റ്)"
         r"|ஒரு\s*(?:நிமிஷம்|நிமிடம்|செகண்ட்|நொடி)"
         r"|\b(?:oru|ore)\s+(?:nimisham|nimidam|minute|second|nodi)\b"),
        # "stay on the line", "लाइन पर रहिए", "bear with me", "കാത്തിരിക്കൂ".
        ("stay_on_line",
         r"\b(?:stay|remain|be|hold)\s+on\s+(?:the\s+)?(?:line|call)\b|\bbear\s+with\s+me\b"
         r"|\bdon'?t\s+(?:go|leave|hang\s+up|disconnect|cut)\b"
         r"|" + _B + r"(?:line|call|लाइन|कॉल)\s*(?:par|pe|pr|पर|पे)\s*(?:hi\s+|ही\s+)?"
         r"(?:rah\w*|reh\w*|ban[ae]\s*rah\w*|ruk\w*|रह" + _W + r"*|बन[ाे]\s*रह" + _W + r"*|रुक" + _W + r"*)"
         r"|കാത്തിരിക്ക|വെയിറ്റ്\s*ചെയ്യ|ലൈനിൽ\s*(?:തുടര|നിൽക്ക|ഉണ്ടാക)"
         r"|காத்திரு|வெயிட்\s*பண்ணு|லைன்ல(?:யே)?\s*இரு"),
        # "मैं देख रहा हूँ", "dhoondh raha hoon", "check karta hoon", "let me check".
        ("busy_looking",
         _B + _DOING + _PROGRESSIVE
         + r"|\blet\s+me\s+(?:just\s+)?(?:check|see|look|find|get|search|open|pull|note)\b"
         r"|\b(?:i\s*am|i'm|im)\s+(?:just\s+)?(?:checking|looking|searching|finding|opening|"
         r"getting|fetching|pulling)\b"
         r"|\b(?:just\s+)?(?:checking|opening|searching)\b(?!\s+(?:account|ac|a/c))"
         r"|\bso\s+that\s+i\s+can\s+(?:check|see|look|find|get)\b"),
        # "बताता हूँ", "abhi batata hoon", "I'll tell you in a moment".
        ("will_tell",
         _B + r"(?:abhi|अभी|ek\s*(?:minute|min|second)|एक\s*(?:मिनट|सेकंड))\s*(?:\S+\s+){0,2}?"
         r"(?:bata|batat|bol|de|bhej|बता|बोल|दे|भेज)" + _W + r"*\s*(?:hoon|hun|hu|हूँ|हूं|हु)"
         r"|" + _B + r"(?:batat[ai]|bataat[ai]|बतात[ाी]|बताता|बताती)\s*(?:hoon|hun|hu|हूँ|हूं|हु)"
         r"|\bi(?:'ll|\s+will)\s+(?:tell|give|share|let)\s+you\b.*\b(?:moment|minute|second|sec)\b"),
    )
)

# Waiting is refused or impossible.
_NEGATED = re.compile(
    _B + r"(?:wait|hold|ruk\w*|intezaa?r|वेट|होल्ड|रुक" + _W + r"*|इंतज़ार|इंतजार)\s*"
    r"(?:nahi|nahin|nhi|नहीं|नही|mat|मत)(?!\s*(?:kat|kaat|cut|काट|कट|rakh|रख|band|बंद))"
    r"|\b(?:can'?t|cannot|can\s*not|won'?t|will\s+not|don'?t\s+want\s+to|not\s+going\s+to|"
    r"no\s+need\s+to)\s+(?:wait|hold)\b"
    r"|" + _B + _TIME + r"\s*(?:hi\s+|ही\s+)?(?:nahi|nahin|nhi|नहीं|नही)\s*(?:hai|है|h)?" + _E
    + r"|\b(?:no|don'?t\s+have(?:\s+any)?)\s+time\b"
    r"|\b(?:why|how\s+long)\b.*\b(?:wait|hold|holding|waiting)\b"
    r"|" + _B + r"(?:kyu\w*|kyo\w*|क्यों|क्यूँ|क्यूं|kitn\w+\s+(?:der|time)|कितन" + _W + r"+\s+(?:देर|टाइम|समय)|"
    r"kab\s+tak|कब\s+तक)\b.*(?:wait|hold|ruk|वेट|होल्ड|रुक)"
    r"|(?:wait|hold|ruk|वेट|होल्ड|रुक)" + _W + r"*\s*(?:kyu\w*|kyo\w*|क्यों|क्यूँ|क्यूं)",
    re.I,
)
# The caller is back before the turn ended: "… हो गया।", "… हाँ जी।", "… हलो।".
_BACK = re.compile(
    r"(?:" + _B + r"(?:ho\s*gaya|ho\s*gayi|mil\s*gaya|mil\s*gayi|aa\s*gaya|le\s*liya|dekh\s*liya|"
    r"हो\s*गया|हो\s*गई|हो\s*गयी|मिल\s*गया|मिल\s*गई|मिल\s*गयी|आ\s*गया|ले\s*लिया|देख\s*लिया|"
    r"haan?\s*ji|han\s*ji|ji\s*haan?|हाँ\s*जी|हां\s*जी|जी\s*हाँ|जी\s*हां|"
    r"boliye|bolo|bataiye|batao|बोलिए|बोलिये|बोलो|बताइए|बताइये|बताओ|"
    r"hello|hallo|helo|हेलो|हैलो|हलो|"
    r"i'?m\s+back|i\s+am\s+back|got\s+it|found\s+it|done|ready|go\s+ahead|tell\s+me)"
    r")[\s.!?,।॥]*$",
    re.I,
)
# The turn carries the answer itself: an identifier / number being dictated.
_ANSWER_DIGITS = re.compile(r"\d(?:[\s-]?\d){3,}")


def hold_evidence(text: str) -> str | None:
    """The kind of wait wording in ``text`` (first match), or None.

    Corroboration only — call it for a turn the decision layer already
    labelled ``hold``.  "एक मिनट" inside a sentence is not a hold by itself."""
    value = (text or "").strip()
    if not value:
        return None
    for name, pattern in _EVIDENCE:
        if pattern.search(value):
            return name
    return None


# "Give me a time" that is a slot, not a pause.
_SCHEDULING = re.compile(
    _B + r"(?:kal|aaj|parso|subah|sham|shaam|dopahar|raat|baje|tarikh|taarikh|din|hafte|mahine|"
    r"appointment|slot|visit|schedule|date|tomorrow|today|tonight|morning|evening|afternoon|"
    r"night|o'?clock|am|pm|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"somvar|mangalvar|budhvar|guruvar|shukravar|shanivar|ravivar|"
    r"payment|pay|paying|bhar\w*|jama|emi|kist|paisa|paise|salary|"
    r"कल|आज|परसों|सुबह|शाम|दोपहर|रात|बजे|तारीख|दिन|हफ्ते|महीने|अपॉइंटमेंट|स्लॉट|विजिट|"
    r"सोमवार|मंगलवार|बुधवार|गुरुवार|शुक्रवार|शनिवार|रविवार|पेमेंट|किस्त|पैसा|पैसे)" + _E
    + r"|" + _B + r"(?:ka|ki|ke|का|की|के|kaun\s*sa|कौन\s*सा|kya|क्या|which|what)\s+" + _TIME + _E
    # "kitna time lagega?", "how much time will it take?" ask about a duration.
    + r"|" + _B + r"(?:kitn\w+|कितन" + _W + r"+|how\s+(?:much|long)|kab\s+tak|कब\s+तक)" + _E,
    re.I,
)
_EXPLICIT_KINDS = ("time_request", "stay_on_line")


def explicit_wait_request(text: str) -> str | None:
    """Wording that asks the bot for time or to stay on the line — strong
    enough to stand WITHOUT a model label (a turn the model never read).

    Softer wording ("एक मिनट", "देख रहा हूँ", "बताता हूँ") stays
    corroboration-only: by itself it is an ordinary sentence."""
    value = (text or "").strip()
    if not value or hold_counter_evidence(value) or _SCHEDULING.search(value):
        return None
    for name, pattern in _EVIDENCE:
        if name in _EXPLICIT_KINDS and pattern.search(value):
            return name
    return None


def hold_counter_evidence(text: str) -> str | None:
    """Why ``text`` is not a wait request despite wait wording, or None."""
    value = (text or "").strip()
    if not value:
        return None
    if _NEGATED.search(value):
        return "negated"
    if _ANSWER_DIGITS.search(value):
        return "answer_digits"
    if _BACK.search(value):
        return "caller_back"
    return None


__all__ = ["explicit_wait_request", "hold_counter_evidence", "hold_evidence"]
