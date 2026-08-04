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
# reasons about. Hindi (Devanagari), Hinglish (Latin) and English are covered
# by every pattern. ORDER MATTERS:
#  - complaints about the conversation itself outrank everything (a caller
#    saying "you are not listening" must never be matched as a refusal),
#  - negated commitments ("nahi karunga") must be seen by hardship/refusal
#    BEFORE the positive payment patterns can match their verb.
#
# NOTE: Python's \b misfires after Devanagari matra-final words — Devanagari
# alternates stay outside \b groups (same convention as detect_hangup above).

_SIGNAL_PATTERNS: list[tuple[str, re.Pattern]] = [
    # The caller says the bot is not listening / keeps repeating itself.
    ("complaint", re.compile(
        r"(?:sun|सुन)\w*\s+(?:(?:hi|ही)\s+)?(?:nahi|nahin|नहीं|नही)"
        r"|(?:nahi|nahin|नहीं|नही)\s+(?:sun|सुन)"
        r"|not listening|listen nahi|(?:samajh|समझ)\w*\s+(?:hi\s+)?"
        r"(?:nahi|nahin|नहीं|नही)\s+(?:rahe|rahi|rhe|rhi|रहे|रही)"
        r"|not understanding me|(?:wahi|वही|same)\s+(?:baat|बात)"
        r"|baar baar|बार बार|(?:repeat|रिपीट)\s+(?:kar|कर|ho|हो)",
        re.I,
    )),
    # The caller did not understand the bot.
    ("clarify", re.compile(
        r"(?:samajh|समझ)(?:\s+(?:mein|में))?\s+(?:nahi|nahin|नहीं|नही)\s+"
        r"(?:aaya|aayi|आया|आयी|आई)"
        r"|matlab kya|kya matlab|kya (?:kaha|bola)|मतलब क्या|क्या मतलब"
        r"|क्या (?:कहा|बोला)|didn'?t (?:under)?stand|did not understand",
        re.I,
    )),
    # Claims the payment was already made.
    ("already_paid", re.compile(
        r"already paid"
        r"|(?:payment|पेमेंट|paisa|paise|पैसा|पैसे|amount)\W+(?:\w+\W+){0,4}"
        r"(?:kar (?:di|diya|chuka|chuki)|ho (?:gaya|gayi|chuka|chuki)|"
        r"kat (?:gaya|gayi)|bhar (?:diya|di)|कर (?:दी|दिया|चुका|चुकी)|"
        r"हो (?:गया|गई|चुका|चुकी)|कट (?:गया|गई)|भर (?:दिया|दी))"
        r"|^\W*(?:paid|kar (?:di|diya|chuka|chuki)|ho (?:chuki|chuka|gaya|gayi)|"
        r"kat (?:gaya|gayi)|कर (?:दी|दिया|चुका|चुकी)|हो (?:चुकी|चुका|गया|गई)|"
        r"कट (?:गया|गई))\W*$",
        re.I,
    )),
    # Wrong person / not my loan.
    ("wrong_person", re.compile(
        r"galat number|wrong number|main (?:woh|wo|vo) nahi|koi aur"
        r"|mera loan nahi|loan (?:liya hi nahi|nahi liya)|is naam"
        r"|गलत नंबर|मैं (?:वो|वह) नहीं|कोई और|मेरा लोन नहीं|इस नाम",
        re.I,
    )),
    # Wants a human.
    ("agent_request", re.compile(
        r"\b(?:agent|customer care|supervisor|manager|human|representative)\b"
        r"|insaan se|aadmi se|एजेंट|कस्टमर केयर|इंसान से|आदमी से|सुपरवाइज़र|मैनेजर",
        re.I,
    )),
    # Financial / medical hardship — cannot pay.
    ("hardship", re.compile(
        r"(?:paisa|paise|money|funds|पैसा|पैसे)\s*(?:hi\s+|ही\s+)?"
        r"(?:bhi\s+|भी\s+)?(?:nahi|nahin|नहीं|नही)"
        r"|no money|can ?not (?:pay|afford)|can'?t (?:pay|afford)"
        r"|afford nahi|(?:payment|पेमेंट|pay|पे|bhugtan|भुगतान)\s+"
        r"(?:nahi|nahin|नहीं|नही)\s+(?:kar|कर|de|दे|ho|हो)"
        r"|(?:nahi|nahin|नहीं|नही)\s+(?:de|दे|bhar|भर)\s+"
        r"(?:sakta|sakti|paunga|paungi|sakenge|सकता|सकती|पाऊंगा|पाऊँगा|पाऊंगी)"
        r"|financial (?:problem|difficulty|issue)|आर्थिक|वित्तीय"
        r"|paise ki (?:dikkat|kami|tangi)|पैसों? की (?:दिक्कत|कमी|तंगी)"
        r"|medical emergency|hospital|bimaar|bimar|beemar|ilaaj|ilaj"
        r"|बीमार|बिमार|अस्पताल|इलाज|मेडिकल"
        r"|(?:naukri|job|नौकरी)\s*(?:nahi|nahin|chali gayi|chhut|khatam|नहीं|चली गई|छूट)"
        r"|(?:salary|सैलरी|pagar|पगार|tankhwah|तनख्वाह)\s*(?:nahi|nahin|नहीं|नही)"
        r"|berozgar|बेरोज़गार|बेरोजगार|majboori|majburi|मजबूरी|मज़बूरी",
        re.I,
    )),
    # Busy now / call me later.
    ("callback", re.compile(
        r"call ?back|baad (?:mein|me|में)|बाद में"
        r"|(?:kal|parso|कल|परसों)\s+(?:call|karunga|karungi|kar|karo|कॉल|करूंगा|करूंगी|कर)"
        # "शाम को कॉल करना", "subah call karo" — a time + an imperative call.
        r"|(?:shaam|sham|subah|dopahar|शाम|सुबह|दोपहर)\s*(?:ko|को)?\s*"
        r"(?:call|कॉल|phone|फोन)"
        r"|(?:call|कॉल|phone|फोन)\s*(?:kar(?:na|o|iye)|karn[ae]|करना|करो|कीजिए|kijiye)"
        r"|\bbusy\b|meeting|vyast|व्यस्त|मीटिंग|gaadi chala|गाड़ी चला|driv(?:e|ing)"
        r"|(?:baat|बात)\s+(?:nahi|nahin|नहीं|नही)\s+kar\s+(?:sakta|sakti|सकता|सकती)"
        r"|time chahiye|samay chahiye|समय चाहिए|टाइम चाहिए|more time"
        r"|(?:agle|अगले)\s+(?:hafte|week|mahine|हफ़्ते|हफ्ते|महीने)",
        re.I,
    )),
    # A question about amounts / process / consequences.
    ("question", re.compile(
        r"kitn[aei]\w*|कितन[ाेी]?"
        r"|^\s*(?:kya|kab|kaise|kyun|kyon|kahan|क्या|कब|कैसे|क्यों|कहाँ|कहां)\b"
        r"|\?\s*$",
        re.I,
    )),
    # Refusal — negated commitment or a bare "no".
    ("refusal", re.compile(
        r"(?:nahi|nahin|नहीं|नही)\s+(?:karunga|karungi|karta|hoga|dunga|dungi|"
        r"करूंगा|करूंगी|करता|होगा|दूंगा|दूंगी)"
        r"|(?:mana|इनकार|इन्कार)\s*(?:kar|कर)"
        r"|^\W*(?:abhi|अभी|filhaal|फ़िलहाल|फिलहाल)?\W*(?:to|तो)?\W*"
        r"(?:bilkul|बिल्कुल)?\W*(?:nahi|nahin|no|nope|नहीं|नही)"
        r"(?:\s*(?:nahi|nahin|नहीं|नही|ji|जी))?\W*$",
        re.I,
    )),
    # Positive commitment to pay (verbs, not the bare noun "payment").
    ("payment_intent", re.compile(
        r"(?:payment|पेमेंट|pay|पे|bhugtan|भुगतान|paisa|paise|पैसा|पैसे|amount)\s+"
        r"(?:\w+\s+)?(?:kar|कर|bhar|भर)\w*"
        r"|(?:kar|कर)\s*(?:dunga|dungi|deta|deti|दूंगा|दूंगी|देता|देती)"
        r"|karunga|karungi|करूंगा|करूंगी"
        r"|\b(?:upi|bhim|paytm|g ?pay|google pay|phone ?pe|debit|card|atm)\b"
        r"|यूपीआई|भीम|पेटीएम|फोन ?पे|गूगल ?पे|डेबिट|कार्ड|एटीएम"
        r"|i (?:will|can) pay|ready to pay|taiyar|तैयार",
        re.I,
    )),
    # A bare confirmation ("haan", "theek hai") — meaningful only in context.
    ("affirm", re.compile(
        r"^\W*(?:haan(?: ji)?|han ?ji|haanji|ji haan|ji|yes|yeah|ok(?:ay)?(?: ji)?|"
        r"theek(?: hai)?|thik(?: hai)?|bilkul|zaroor|jarur|sahi(?: hai)?|sure|"
        r"हाँ|हां|जी(?: हाँ| हां)?|ठीक(?: है)?|बिल्कुल|ज़रूर|जरूर|सही(?: है)?|"
        r"ओके(?: जी)?|अच्छा)\W*$",
        re.I,
    )),
]


def classify_user_signal(text: str) -> str | None:
    """Semantic signal of an utterance: hardship, refusal, complaint,
    clarify, callback, payment_intent, already_paid, wrong_person,
    agent_request, question, affirm — or None. Language-agnostic across
    Hindi/Hinglish/English; deliberately conservative (None over a guess)."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    for name, pattern in _SIGNAL_PATTERNS:
        if pattern.search(stripped):
            return name
    return None


_SMALLTALK = re.compile(
    r"^\s*(hi|hii+|hello|hey|good (morning|afternoon|evening)|namaste|"
    r"thanks?( you)?( so much)?|thank you|ok(ay)?|yes|yeah|no|nope|sure|great|"
    r"bye|goodbye|see you|talk (to you )?later)( there| everyone| all)?\s*[.!?]*\s*$",
    re.IGNORECASE,
)

# ── multilingual hang-up detection ──────────────────────────────────────────
# Deterministic, transcription-tolerant matching for Hindi (Devanagari),
# Hinglish (Latin) and English. Checked before EVERYTHING else (including an
# active workflow) — a caller asking to hang up must never receive another
# payment pitch, clarification or LLM fallback.
#
# NOTE: Python's \b misfires after Devanagari matra-final words — Devanagari
# alternates stay outside \b groups.

# Negations must never hang up: "फोन मत काटो", "call mat kato", "don't hang
# up". Both orders are covered (neg before verb, and "kaatna mat").
_HANGUP_NEGATION = re.compile(
    r"(?:\bmat\b|\bna\b|\bnahin?\b|\bdon'?t\b|\bdo not\b|मत|ना|नहीं)\W*"
    r"(?:\w+\W+)?(?:kat+\w*|kaat\w*|cut|band|bandh|khat[ae]?m|rakh\w*|hang|"
    r"disconnect|काट\w*|कट|बंद|ख़?त्म|रख)"
    r"|(?:kat+n[aei]|kaatn[aei]|काटना|कट करना)\W+(?:mat\b|मत)",
    re.I,
)

_HANGUP_PATTERNS: list[re.Pattern] = [
    # English.
    re.compile(
        r"\b(hang ?up|end (the |this )?call|disconnect( the| this)?( call| phone)?|"
        r"(cut|stop|drop) (the |this )?call)\b",
        re.I,
    ),
    # phone/call + cut/band/khatam/rakh verb (Latin and Devanagari nouns).
    re.compile(
        r"(?:\b(?:phone|phon|fone|call|kaal)\b|फ़?ोन|फ़ोन|फोन|कॉल|काल)\W*(?:ko\W+|को\W*)?"
        r"(?:kat+\w*|kaat\w*|cut|band\w*|khat[ae]?m|khatm|rakh\w*|काट\w*|कट|बंद|ख़?त्म|रख)",
        re.I,
    ),
    # Bare imperative cut verb: "cut kar do", "cut karo", "cut karu",
    # "kaat do", "काट दो", "कट करो". Past tense ("paise kat gaye" — money got
    # deducted) deliberately does NOT match: only imperative aux verbs listed.
    re.compile(
        r"\b(?:kat+|kaat|cut)\s+(?:kar\w*|kr\w*|do|de|dijiye|dena)\b",
        re.I,
    ),
    re.compile(r"(?:काट|कट)\s*(?:कर\s*)?(?:दो|दे|दीजिए|करो|करिए)"),
    # band/khatam without a phone/call noun needs a "bas" style terminator so
    # "SMS band karo" (stop the messages) can't kill the call.
    re.compile(
        r"\bbas\b.{0,16}\b(?:band|bandh|khat[ae]?m|khatm)\s+kar\w*",
        re.I,
    ),
    re.compile(r"बस.{0,16}(?:बंद|ख़?त्म)\s*कर"),
]


def detect_hangup(text: str) -> bool:
    """Deterministic multilingual hang-up intent (hi / hinglish / en)."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if _HANGUP_NEGATION.search(stripped):
        return False
    return any(p.search(stripped) for p in _HANGUP_PATTERNS)


# ── do-not-call / emergency / consent refusal (platform-critical) ────────────
# Like hang-up these are deterministic, multilingual and checked before any
# workflow, intent model or LLM: a caller revoking contact consent or
# reporting an emergency must never receive another pitch first.

_DNC_PATTERNS: list[re.Pattern] = [
    re.compile(
        r"\b(?:do ?n[o']t|never|stop) call(?:ing)?( me| again| back)?\b"
        r"|\bremove (?:my|this) number\b|\bstop (?:these|the) calls\b"
        r"|\btake me off\b|\bunsubscribe\b",
        re.I,
    ),
    # "dobara/phir/aage call mat karna", "फिर मत करना कॉल"
    re.compile(
        r"(?:dobara|dubara|phir|firse|fir se|aage|kabhi|दोबारा|दुबारा|फिर|आगे|कभी)\s*"
        r"(?:se\s*|से\s*)?(?:call|phone|कॉल|फोन)?\s*(?:mat|मत|na|नहीं|nahi)\s*"
        r"(?:kar|कर|karna|करना|karo|करो)",
        re.I,
    ),
    re.compile(
        r"(?:call|phone|कॉल|फोन)\s*(?:mat|मत)\s*(?:kar|कर)\w*"
        r"|(?:mat|मत)\s*(?:karo|करो|karna|करना)\s*(?:call|phone|कॉल|फोन)",
        re.I,
    ),
]


def detect_do_not_call(text: str) -> bool:
    """Deterministic 'never call me again' consent revocation."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    return any(p.search(stripped) for p in _DNC_PATTERNS)


_EMERGENCY = re.compile(
    r"\bemergency\b|\bambulance\b|\bpolice\b|heart attack|accident (?:ho|हो)"
    r"|(?:mar|मर) (?:raha|rahi|रहा|रही)|suicide|khudkushi|आत्महत्या"
    r"|एम्बुलेंस|इमरजेंसी|पुलिस|एक्सीडेंट",
    re.I,
)


def detect_emergency(text: str) -> bool:
    """Emergency / safety language — escalate to a human, never a pitch."""
    stripped = (text or "").strip()
    return bool(stripped) and bool(_EMERGENCY.search(stripped))


# "don't record", "recording band karo" — consent refusal for recording.
_CONSENT_REFUSAL = re.compile(
    r"(?:do ?n[o']t|stop|no)\s+record(?:ing)?"
    r"|record(?:ing)?\s*(?:mat|मत|band|बंद)\s*(?:kar|कर)?"
    r"|रिकॉर्ड(?:िंग)?\s*(?:मत|बंद)"
    r"|consent\s+(?:nahi|नहीं|withdraw)",
    re.I,
)


def detect_consent_refusal(text: str) -> bool:
    stripped = (text or "").strip()
    return bool(stripped) and bool(_CONSENT_REFUSAL.search(stripped))


_CALL_CONTROL: list[tuple[re.Pattern, str]] = [
    # hang-up lives in detect_hangup() (multilingual + negation-guarded),
    # checked before this list ever runs.
    (re.compile(r"\b(transfer|connect) (me )?(to )?(a |an )?(human|agent|person|representative|someone)\b", re.I), "transfer"),
    (re.compile(r"\b(speak|talk) (to|with) (a |an )?(human|agent|person|representative)\b", re.I), "transfer"),
    (re.compile(r"\b(repeat|say (that|it) again|pardon|come again)\b", re.I), "repeat"),
    (re.compile(r"\b(speak|talk|go) (more )?slow(ly|er)?\b", re.I), "slower"),
]

_HANDOFF = re.compile(r"\b(human|agent|supervisor|manager|representative)\b", re.I)

# Question shapes that usually need tenant knowledge.
_KB_SIGNALS = re.compile(
    r"\b(what|how|when|where|which|why|can i|do you|is there|are there|"
    r"policy|policies|coverage|premium|claim|deadline|grace period|charges?|"
    r"fees?|interest|rate|document|procedure|process|eligib|terms?|"
    r"conditions?|renewal|refund|cancel(lation)?|timings?|hours|address)\b",
    re.I,
)

_UNSAFE = re.compile(
    r"\b(card number|cvv|otp|one[- ]time password|password) (is|was)?\s*[:\-]?\s*\d",
    re.I,
)


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
        self._workflows = workflows or {}

    def decide(self, text: str, *, active_workflow: str | None = None) -> RouteDecision:
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

        # 3. Configured intents (sample keyword voting). This must precede the
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

    def _match_intent(self, text: str) -> tuple[str, str | None, float] | None:
        lowered = text.lower()
        best: tuple[str, str | None, float] | None = None
        for intent in self._intents:
            samples = [s.lower() for s in (intent.get("samples") or [])]
            if not samples:
                continue
            hits = sum(1 for s in samples if s and s in lowered)
            score = hits / len(samples) if samples else 0.0
            threshold = float(intent.get("confidence_threshold") or 0.5)
            # A single exact sample phrase match is a strong signal.
            if hits and (score >= threshold or any(s == lowered for s in samples)):
                confidence = max(score, 0.9 if any(s == lowered for s in samples) else score)
                if best is None or confidence > best[2]:
                    best = (intent.get("name", "intent"), intent.get("route"), confidence)
        return best
