"""Core signal pack — conversation-control meanings every bot needs.

Base patterns are the platform's Hinglish/English baseline; Malayalam, Tamil
and any later language add their forms through language-pack fragments.
Priorities reproduce the historical single-list order exactly.
"""
from shared.orchestration.signals import Composer, SignalPack, SignalSpec

HINGLISH_BASELINE = ("hi", "en")


def _refusal(ctx: Composer) -> str:
    """Negated commitment or a bare 'no' (Hinglish/English base), plus one
    bare-negation clause for every other language: repeated negations with
    an optional courtesy tail are still one refusal."""
    base = _REFUSAL_BASE
    others = ctx.other_codes(HINGLISH_BASELINE)
    if not others:
        return base
    no_tokens = ctx.alternatives_of("no_tokens", others)
    if not no_tokens:
        return base
    tails = ctx.alternatives_of("courtesy_tails", ("en", *others))
    return (base + r"|^\W*(?:" + no_tokens + r")(?:\W+(?:" + no_tokens + r"))*"
            + (r"(?:\W+(?:" + tails + r"))?" if tails else "") + r"\W*$")


def _affirm(ctx: Composer) -> str:
    """A bare confirmation, repeated up to four times ("haan haan", "yes
    yes"), optionally closed by a courtesy word — in every registered language."""
    tokens = ctx.alternatives("affirm_tokens")
    tails = ctx.alternatives("courtesy_tails")
    return (r"^\W*(?:(?:" + tokens + r")\W*){1,4}"
            + (r"(?:" + tails + r")?" if tails else "") + r"\W*$")


_REFUSAL_BASE = r"""(?:nahi|nahin|नहीं|नही)\s+(?:karunga|karungi|karta|hoga|dunga|dungi|करूंगा|करूंगी|करता|होगा|दूंगा|दूंगी)|(?:nahi|nahin|नहीं|नही)(?:\s+\w+){0,2}\s+(?:kar|कर)\s+(?:raha|rahi|रहा|रही|riha|रिहा)|(?:mana|इनकार|इन्कार)\s*(?:kar|कर)|^\W*(?:abhi|अभी|filhaal|फ़िलहाल|फिलहाल)?\W*(?:to|तो)?\W*(?:bilkul|बिल्कुल)?\W*(?:nahi|nahin|no|nope|नहीं|नही)(?:\W+(?:nahi|nahin|no|nope|नहीं|नही|ji|जी))*(?:\W+(?:please|pls|thanks|thank you|dhanyavaad|dhanyawad|shukriya|धन्यवाद|शुक्रिया))?\W*$"""

PACK = SignalPack(
    name="core",
    specs=(
    # The caller says the bot is not listening / keeps repeating itself.
    SignalSpec(
        name='complaint', priority=10, off_script=True,
        pattern=r"""(?:sun|सुन)\w*\s+(?:(?:hi|ही)\s+)?(?:nahi|nahin|नहीं|नही)|(?:nahi|nahin|नहीं|नही)\s+(?:sun|सुन)|not listening|listen nahi|(?:samajh|समझ)\w*\s+(?:hi\s+)?(?:nahi|nahin|नहीं|नही)\s+(?:rahe|rahi|rhe|rhi|रहे|रही)|not understanding me|(?:wahi|वही|same)\s+(?:baat|बात)|baar baar|बार बार|(?:repeat|रिपीट)\s+(?:kar|कर|ho|हो)""",
    ),
    # The caller did not understand the bot.
    SignalSpec(
        name='clarify', priority=20, off_script=True,
        pattern=r"""(?:samajh|समझ)(?:\s+(?:mein|में))?\s+(?:nahi|nahin|नहीं|नही)\s+(?:aaya|aayi|आया|आयी|आई)|matlab kya|kya matlab|kya (?:kaha|bola)|मतलब क्या|क्या मतलब|क्या (?:कहा|बोला)|didn'?t (?:under)?stand|did not understand""",
    ),
    # Wrong number / not the person asked for (domain packs extend: 'not my loan').
    SignalSpec(
        name='wrong_person', priority=40, entry=True,
        pattern=r"""galat number|wrong number|main (?:woh|wo|vo) nahi|koi aur|is naam|गलत नंबर|मैं (?:वो|वह) नहीं|कोई और|इस नाम""",
    ),
    # Wants a human.
    SignalSpec(
        name='agent_request', priority=50, entry=True,
        pattern=r"""\b(?:agent|customer care|supervisor|manager|human|representative)\b|insaan se|aadmi se|एजेंट|कस्टमर केयर|इंसान से|आदमी से|सुपरवाइज़र|मैनेजर""",
    ),
    # "Wait a moment / stay on the line" — a HOLD, not a callback.
    SignalSpec(
        name='hold', priority=70, off_script=True,
        pattern=r"""^(?!.*(?:call ?back|call (?:me )?later|(?<![\wऀ-ॿ])(?:baad (?:mein|me)|बाद में)|(?:call|कॉल|phone|फोन)\s*(?:kar(?:na|o|iye)|karn[ae]|करना|करो|कीजिए|kijiye)))(?=.*(?:(?:ek|do|एक|दो|one|two|a|paanch|panch|पाँच|पांच|thoda|थोड़ा|\d+)?\s*(?:minutes?|mins?|mint|seconds?|secs?|moment|मिनट|मिनिट|सेकंड|सेकेंड|पल)\s*(?:ruk|रुक|hold|wait|do(?![\w])|दो|dijiye|दीजिए|dena|देना|de(?![\w])|दे(?![\wऀ-ॿ]))|^\W*(?:haan\s+|हाँ\s+|ji\s+|जी\s+|bas\s+|बस\s+|just\s+)?(?:ek|एक|one|1|do|दो|two|2|a)\s*(?:minute|min|mint|मिनट|मिनिट|second|sec|सेकंड|moment)\W*$|(?<![\wऀ-ॿ])(?:ruk(?:o|iye|iyega|na|\s+ja(?:o|iye|na)?)|रुको|रुकिए|रुकिये|रुकना|रुक\s*जा(?:ओ|इए|ना)?|thehr\w*|ठहर\w*)|\bhold(?:\s+on|\s+karo|\s+kijiye|\s+the\s+line)?\b|होल्ड|\bhang\s+on\b|\bwait(?:\s+(?:a\s+)?(?:minute|moment|second|sec|karo|kijiye|kar))?\b(?!ing)|वेट|(?:line|लाइन)\s*(?:par|pe|pr|पर|पे)|(?:rakh|रख)(?:na|o|iye|ना|ो|िए)?\s*(?:mat|मत|nahi|nahin|नहीं|नही)|(?:kat+\w*|kaat\w*|cut|काट\w*|कट|band|बंद)\s*(?:mat|मत|na|ना|nahi|nahin|नहीं|नही)|(?:mat|मत)\s*(?:kat+\w*|kaat\w*|cut|काट\w*|कट|rakh\w*|रख\w*|band|बंद)|don'?t\s+(?:hang\s+up|disconnect|cut|go)|do\s+not\s+(?:hang\s+up|disconnect|cut)|(?:abhi|अभी)\s*(?:aata|aaya|aati|aayi|आता|आया|आती|आई)))""",
    ),
    # Busy now / call me later.
    SignalSpec(
        name='callback', priority=80, entry=True, literal_fallback=True,
        pattern=r"""call ?back|call (?:me )?later|(?<![\wऀ-ॿ])(?:baad (?:mein|me|में)|बाद में)|(?<![\wऀ-ॿ])(?:kal|parso|कल|परसों)\s+(?:call|karunga|karungi|kar|karo|कॉल|करूंगा|करूंगी|कर)|(?:shaam|sham|subah|dopahar|शाम|सुबह|दोपहर)\s*(?:ko|को)?\s*(?:call|कॉल|phone|फोन)|(?:call|कॉल|phone|फोन)\s*(?:kar(?:na|o|iye)|karn[ae]|करना|करो|कीजिए|kijiye)|\bbusy\b|meeting|vyast|व्यस्त|मीटिंग|gaadi chala|गाड़ी चला|driv(?:e|ing)|(?:baat|बात)\s+(?:nahi|nahin|नहीं|नही)\s+kar\s+(?:sakta|sakti|सकता|सकती)|time chahiye|samay chahiye|समय चाहिए|टाइम चाहिए|more time|(?:agle|अगले)\s+(?:hafte|week|mahine|हफ़्ते|हफ्ते|महीने)""",
    ),
    # A question about amounts / process / consequences.
    SignalSpec(
        name='question', priority=90, off_script=True,
        pattern=r"""kitn[aei]\w*|कितन[ाेी]?|^\s*(?:kya|kab|kaise|kyun|kyon|kahan|क्या|कब|कैसे|क्यों|कहाँ|कहां)\b|\?\s*$""",
    ),
    SignalSpec(
        name="refusal", priority=100, entry=True, literal_fallback=True, yes_no=True, generic_answer=True,
        template=_refusal,
    ),
    SignalSpec(
        name="affirm", priority=120, literal_fallback=True, yes_no=True, generic_answer=True,
        template=_affirm,
    ),
    ),
    knowledge_terms=(
        "deadline", "charges?", "fees?", "document", "procedure", "process", "eligib", "terms?",
        "conditions?", "refund", "cancel(lation)?", "timings?", "hours", "address",
    ),
)
