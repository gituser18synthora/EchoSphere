"""Collection-call conversation policy — explicit state over prompt hope.

The recurring live failures this module exists to prevent are all of one
shape: the scripted collection ladder (workflow rungs) kept advancing or
repeating while the customer had said something that should have changed the
conversation — "this is not my loan", "I already paid", "call me later",
"you are not listening", "who is this?". The published prompt asked the LLM
nicely; nothing *enforced* it.

:class:`CollectionCallPolicy` tracks the call as explicit state:

- phases: greeting → identity verification → account explanation → payment
  discussion, with interrupt states (payment already made, account dispute,
  wrong party, complaint, callback, escalation) and a closing state;
- **blockers**: identity mismatch, account dispute, payment-already-made
  claim, complaint. While one is open the workflow ladder is force-paused
  (the brain routes the turn to the LLM with this policy's instruction
  instead of advancing/repeating a rung) and payment persuasion is
  prohibited;
- verified facts come only from the server-loaded
  :class:`~shared.customer_context.CustomerContextSnapshot` (already masked);
  customer statements from the call are tracked separately as *unverified
  claims*; the LLM prompt keeps the two apart;
- amounts / dates / account details are withheld from the prompt until the
  customer's identity is confirmed — what the model does not have it cannot
  leak;
- every turn produces a :class:`TurnPlan`: whether the LLM must answer
  (instead of the workflow), whether the call should end after the reply,
  a deterministic handoff, and the per-turn instruction block;
- **identity is confirmed only by an anchored clear yes** (see
  :func:`classify_identity_answer`); ambiguous, partial or noisy answers get
  a scripted re-ask — the LLM never gets to guess who answered;
- an already-paid claim runs a **transaction-reference state machine**
  (claimed → awaiting_transaction_reference → verifying → verified /
  pending / failed / unverified): "हाँ, नंबर है" is never the number, only a
  captured value is ever called noted, and only a TOOL result is ever called
  verified — the outcome replies are scripted from that result;
- the model always receives the derived spec-level
  :func:`~CollectionCallPolicy.conversation_state`, the allowed / prohibited
  actions for it, and the required fields still missing; the brain validates
  actions (:func:`~CollectionCallPolicy.validate_action`) and gates every
  close through :func:`~CollectionCallPolicy.evaluate_completion` — a polite
  goodbye alone never completes a call;
- the terminal state maps to a stored **disposition** and to the call-state
  fields written back to the customer context row (including the structured
  :func:`~CollectionCallPolicy.payment_record`).

Understanding vs. enforcement — the two layers are deliberately separate:

- **Understanding** (what the caller meant) comes from the Goal Engine's
  validated :class:`~shared.orchestration.decision_schema.ConversationDecision`
  whenever one is available: identity confirm/deny/ambiguous, slot
  observations ("has a number" vs "said the number"), the generic signal.
  The regex patterns kept below run ONLY as the deterministic fallback for
  turns where the agentic path could not produce a decision (LLM down,
  timeout, unparseable output) — they are no longer the primary mechanism.
- **Enforcement** (what may happen next) stays fully deterministic here:
  phases, blockers, the transaction-reference state machine,
  :func:`~CollectionCallPolicy.validate_action`,
  :func:`~CollectionCallPolicy.evaluate_completion` and identity-gated
  disclosure never move on model output alone.

Scripted ``collections_*`` phrases are last-resort fallbacks for when the
LLM cannot generate (they also keep this domain's wording OUT of shared
orchestration code); on the agentic path the same turns are generated from
the per-turn instruction with the scripted text as the safety net.
"""

import re
import time
from dataclasses import dataclass, field, replace as dataclass_replace

from shared.customer_context import CustomerContextSnapshot
from shared.orchestration.decision_schema import SCOPE_IN, ConversationDecision
from shared.orchestration.phrases import resolve_phrase
from shared.orchestration.spoken_numbers import verbalized_digits

# ── phases ───────────────────────────────────────────────────────────────────
GREETING = "greeting"
RECORDING_NOTICE = "recording_notice"
IDENTITY_VERIFICATION = "identity_verification"
ACCOUNT_EXPLANATION = "account_explanation"
PAYMENT_DISCUSSION = "payment_discussion"
PAYMENT_ALREADY_MADE = "payment_already_made"
ACCOUNT_DISPUTE = "account_dispute"
WRONG_PARTY = "wrong_party"
COMPLAINT_HANDLING = "complaint_handling"
CALLBACK_REQUESTED = "callback_requested"
ESCALATION = "escalation"
CLOSING = "closing"

# ── conversation states (spec-level, derived — see conversation_state()) ─────
AWAITING_IDENTITY_CONFIRMATION = "awaiting_identity_confirmation"
IDENTITY_CONFIRMED = "identity_confirmed"
IDENTITY_UNCLEAR = "identity_unclear"
WRONG_PERSON = "wrong_person"
RECOVERY_DISCUSSION = "recovery_discussion"
PAYMENT_CLAIMED = "payment_claimed"
AWAITING_TRANSACTION_REFERENCE = "awaiting_transaction_reference"
VERIFYING_PAYMENT = "verifying_payment"
PAYMENT_VERIFIED = "payment_verified"
PAYMENT_VERIFICATION_FAILED = "payment_verification_failed"
PAYMENT_VERIFICATION_PENDING = "payment_verification_pending"
PAYMENT_COMMITMENT = "payment_commitment"
CALL_COMPLETED = "call_completed"

# How many unclear identity answers earn re-asks before the call is closed
# WITHOUT verification (and therefore without any account disclosure).
_MAX_IDENTITY_REASKS = 3
# How many unusable transaction-reference answers before the claim is closed
# honestly ("noted, the team will verify") instead of looping forever.
_MAX_REFERENCE_ATTEMPTS = 2

# ── collections fallback phrases (domain-owned; NOT in shared orchestration) ─
# Spoken only when the agentic path cannot generate this turn's reply (LLM
# unavailable / failed) or when a decision could not be produced at all.
# {amount}/{days}/{name}/{reference} are filled from verified context; the
# {reference} is pre-spaced digit by digit for the TTS.
_COLLECTIONS_FALLBACKS: dict[str, dict[str, str]] = {
    "collections_open_amount_days": {
        "en": (
            "There is an overdue payment of {amount} on your account, "
            "pending for {days} days. I'm calling about that payment — "
            "can you pay today?"
        ),
        "hi": (
            "आपके अकाउंट पर {amount} का payment {days} दिनों से overdue है। "
            "मैं इसी payment के लिए call कर रहा हूँ — क्या आप आज payment "
            "कर पाएंगे?"
        ),
    },
    "collections_open_amount": {
        "en": (
            "There is an overdue payment of {amount} on your account. "
            "I'm calling about that payment — can you pay today?"
        ),
        "hi": (
            "आपके अकाउंट पर {amount} का payment overdue है। मैं इसी payment "
            "के लिए call कर रहा हूँ — क्या आप आज payment कर पाएंगे?"
        ),
    },
    "collections_identity_reask": {
        "en": "Sorry — am I speaking with {name}?",
        "hi": "माफ़ कीजिए, क्या मैं {name} जी से बात कर रहा हूँ?",
    },
    "collections_identity_unverified_close": {
        "en": (
            "I'm sorry, I couldn't confirm I'm speaking with the right "
            "person, so I can't discuss this call's purpose. We'll reach "
            "out again later. Thank you."
        ),
        "hi": (
            "माफ़ कीजिए, मैं पुष्टि नहीं कर पाया कि मेरी बात सही व्यक्ति से "
            "हो रही है, इसलिए मैं इस कॉल का विवरण साझा नहीं कर सकता। हम "
            "बाद में दोबारा संपर्क करेंगे। धन्यवाद।"
        ),
    },
    "collections_ask_reference": {
        "en": (
            "Thank you. To verify the payment, please tell me the "
            "transaction or UTR number."
        ),
        "hi": (
            "धन्यवाद। पेमेंट की पुष्टि के लिए कृपया ट्रांजैक्शन या UTR "
            "नंबर बताइए।"
        ),
    },
    "collections_ask_reference_retry": {
        "en": (
            "Sorry, I didn't get the number. Please say the transaction "
            "number slowly, digit by digit."
        ),
        "hi": (
            "माफ़ कीजिए, नंबर समझ नहीं आया। कृपया ट्रांजैक्शन नंबर "
            "धीरे-धीरे, एक-एक अंक करके बताइए।"
        ),
    },
    "collections_payment_verified": {
        "en": (
            "Your payment has been received and verified successfully. "
            "Sorry for the reminder call, and thank you!"
        ),
        "hi": (
            "आपका भुगतान सफलतापूर्वक प्राप्त हो गया है और उसकी पुष्टि हो "
            "चुकी है। कॉल के लिए खेद है, धन्यवाद!"
        ),
    },
    "collections_payment_processing": {
        "en": (
            "I've noted transaction number {reference}. Your payment shows "
            "in our records but is still processing — it will reflect on "
            "your account once complete. Thank you!"
        ),
        "hi": (
            "मैंने ट्रांजैक्शन नंबर {reference} नोट कर लिया है। आपका भुगतान "
            "रिकॉर्ड में दिखाई दे रहा है, लेकिन अभी प्रोसेसिंग में है — पूरा "
            "होते ही अकाउंट में दिखेगा। धन्यवाद!"
        ),
    },
    "collections_payment_not_found": {
        "en": (
            "I couldn't verify a payment against transaction number "
            "{reference} right now. I've recorded the number and our team "
            "will re-check it and get back to you. Thank you."
        ),
        "hi": (
            "अभी ट्रांजैक्शन नंबर {reference} से भुगतान की पुष्टि नहीं हो "
            "पा रही है। मैंने नंबर नोट कर लिया है — हमारी टीम इसे दोबारा "
            "जाँच कर आपसे संपर्क करेगी। धन्यवाद।"
        ),
    },
    "collections_verification_unavailable": {
        "en": (
            "I've noted transaction number {reference}. Verification is "
            "still pending — our team will confirm it against the records. "
            "Thank you."
        ),
        "hi": (
            "मैंने ट्रांजैक्शन नंबर {reference} नोट कर लिया है। पुष्टि अभी "
            "बाकी है — हमारी टीम रिकॉर्ड से इसकी जाँच करेगी। धन्यवाद।"
        ),
    },
    "collections_reference_unavailable_close": {
        "en": (
            "No problem. I've recorded that you've made the payment — our "
            "team will verify it from the records and follow up if needed. "
            "Thank you."
        ),
        "hi": (
            "कोई बात नहीं। मैंने नोट कर लिया है कि आपने भुगतान किया है — "
            "हमारी टीम रिकॉर्ड से इसकी जाँच करेगी और ज़रूरत होने पर संपर्क "
            "करेगी। धन्यवाद।"
        ),
    },
}


def canned(key: str, locale: str | None = None) -> str:
    """Collections fallback phrase in the caller's language (domain-owned)."""
    return resolve_phrase(_COLLECTIONS_FALLBACKS, key, locale)

# ── collection-specific utterance patterns (hi / hinglish / en) ─────────────
# The router's `wrong_person` signal covers BOTH "wrong number" and "not my
# loan"; the policy needs to distinguish them: a wrong number ends the call
# with no details, a dispute is recorded and escalated.
_DISPUTE = re.compile(
    r"loan (?:liya hi nahi|nahi liya|lia hi nahi)|(?:koi|कोई)\s*(?:loan|लोन)\s*"
    r"(?:nahi|nahin|नहीं|नही)|लोन (?:लिया ही नहीं|नहीं लिया)|मैंने (?:कोई )?लोन नहीं"
    r"|\bdispute\b|डिस्प्यूट|\bfraud\b|फ्रॉड|धोखा"
    r"|galat (?:amount|rakam)|(?:amount|अमाउंट|राशि|रकम) (?:galat|गलत)"
    r"|itna (?:nahi|nahin) (?:hai|tha)|इतना (?:नहीं|नही) (?:है|था)"
    r"|settle (?:ho gaya|kar diya)|सेटल हो गया",
    re.I,
)
_WRONG_NUMBER = re.compile(
    r"galat number|wrong number|गलत नंबर"
    r"|main (?:woh|wo|vo) nahi|मैं (?:वो|वह) नहीं|koi aur|कोई और"
    r"|is naam (?:ka|ki|se)|इस नाम",
    re.I,
)
# "मेरा नाम तो सुरेश है" / "my name is Suresh" — an identity mismatch even
# though the router sees no signal in it.
_NAME_MISMATCH = re.compile(
    r"(?:mera naam|मेरा नाम)\s+(?:to|तो)?\s*\S+\s*(?:hai|है)"
    r"|my name is\s+\S+"
    r"|(?:naam|नाम)\s+(?:galat|गलत)",
    re.I,
)
# A concrete time offered for a payment/callback ("शाम को", "kal subah",
# "after 6", "6 baje") — enough to CONFIRM a callback instead of re-asking.
_TIME_HINT = re.compile(
    r"शाम|सुबह|दोपहर|कल|परसों|आज|बजे|subah|shaam|sham|dopahar|kal|parso"
    r"|\baaj\b|baje"
    r"|\bevening\b|\bmorning\b|\bafternoon\b|\btomorrow\b|\btonight\b|\btoday\b"
    r"|\b\d{1,2}\s*(?:am|pm|baje|बजे)\b|\bafter\s+\d",
    re.I,
)
# Payment-claim evidence: a transaction/reference id or an explicit mention.
_PAYMENT_REFERENCE = re.compile(
    r"\b(?:utr|txn|transaction|reference|ref(?:erence)? (?:no|number|id))\b"
    r"|ट्रांज़?[ैे]क्शन|रेफ़?रेंस"
    r"|\b\d{6,}\b",
    re.I,
)
# The customer cannot provide the reference ("नंबर नहीं है", "yaad nahi",
# "don't have it") — distinct from a bare refusal to keep talking.
_NO_REFERENCE = re.compile(
    r"(?:number|नंबर|reference|रेफ़?रेंस)\W*(?:\w+\W+){0,2}?(?:nahi|nahin|नहीं|नही)"
    r"|(?:yaad|याद|pata|पता|maloom|मालूम)\s*(?:nahi|nahin|नहीं|नही)"
    r"|don'?t have|do not have|not with me|abhi nahi mil",
    re.I,
)
# Payment method / date the customer claims (recorded, never treated as fact).
_PAYMENT_METHOD = re.compile(
    r"\b(?:upi|g ?pay|google pay|phone ?pe|paytm|bhim|neft|imps|rtgs"
    r"|net ?banking|debit|credit|card|cash)\b"
    r"|यूपीआई|गूगल ?पे|फोन ?पे|पेटीएम|भीम|नेट ?बैंकिंग|डेबिट|क्रेडिट|कार्ड|कैश|नकद",
    re.I,
)
_PAYMENT_DATE = re.compile(
    r"\b(?:yesterday|today|last week|kal|aaj|parso)\b|कल|आज|परसों|पिछले (?:हफ़्ते|हफ्ते)",
    re.I,
)
_METHOD_CANONICAL = (
    (re.compile(r"upi|g ?pay|google|phone ?pe|paytm|bhim|यूपीआई|गूगल|फोन ?पे|पेटीएम|भीम", re.I), "UPI"),
    (re.compile(r"neft|imps|rtgs|net ?banking|नेट ?बैंकिंग", re.I), "BANK_TRANSFER"),
    (re.compile(r"debit|credit|card|डेबिट|क्रेडिट|कार्ड", re.I), "CARD"),
    (re.compile(r"cash|कैश|नकद", re.I), "CASH"),
)
# An affirmative to "shall I connect you to an agent?" must become a real
# handoff — detected against the BOT's previous reply.
_AGENT_OFFER = re.compile(
    r"agent (?:se|से)?\s*(?:connect|जोड़|jod)|एजेंट से|connect you (?:with|to)"
    r"|hamare agent|हमारे (?:agent|एजेंट)",
    re.I,
)
_IDENTITY_QUESTION = re.compile(
    r"(?:baat|बात)[^।?!]{0,50}(?:ho rah|kar rah|हो रह|कर रह)"
    r"|am i speaking|speaking (?:with|to)|is (?:this|that)\s+\S+"
    r"|account holder|अकाउंट होल्डर"
    r"|(?:aap|आप)[^।?!]{0,20}(?:hi|ही)\s*(?:bol|बोल)",
    re.I,
)
# How-much question about the account (total/overdue/minimum/penalty) —
# domain-owned, independent of the platform signal bank: "कितना payment
# करना है?" is an ASK for a figure, never a commitment and never a refusal.
_AMOUNT_QUERY = re.compile(
    r"(?:kitn\w*|कितन\S*)\W+(?:\w+\W+){0,3}?"
    r"(?:amount|payment|paisa|paise|पैसा|पैसे|rupay\w*|रुपये|रुपए|"
    r"den[ae]|देना|देने|bharn[ae]|भरना|भरने|baki|baaki|बाक़ी|बाकी|"
    r"bakaya|बकाया|due|balance)"
    r"|(?:amount|balance|outstanding|overdue|bakaya|बकाया|राशि|रकम)\W+"
    r"(?:\w+\W+){0,2}?(?:kitn\w*|कितन\S*|kya|क्या|batao|bataiye|बताओ|बताइए|बता)"
    r"|\bhow much\b"
    r"|(?:total|kul|कुल)\W+(?:\w+\W+){0,2}?"
    r"(?:amount|outstanding|baki|baaki|bakaya|बकाया|kitn\w*|कितन\S*)"
    r"|(?:minimum|(?:kam|कम)\s*se\s*(?:kam|कम))\W+(?:\w+\W+){0,2}?"
    r"(?:amount|payment|payable|kitn\w*|कितन\S*)",
    re.I,
)
# Which figure the caller asked for; drives the answer-first amount reply.
_AMOUNT_TYPE_PATTERNS = (
    ("total", re.compile(
        r"\btotal\b|kul|कुल|poora|पूरा|\bfull\b|sab (?:kitna|कितना)", re.I)),
    ("minimum", re.compile(
        r"minimum|(?:kam|कम)\s*se\s*(?:kam|कम)|\bpart(?:ial)?\b|थोड़ा", re.I)),
    ("penalty", re.compile(
        r"penalty|late fee|penal|जुर्माना|extra charge|चार्ज", re.I)),
    ("overdue", re.compile(r"overdue|due amount|बकाया|bakaya", re.I)),
)


def detect_amount_query(text: str) -> bool:
    """Whether the utterance asks for an account figure."""
    stripped = (text or "").strip()
    return bool(stripped) and bool(_AMOUNT_QUERY.search(stripped))


# A genuine medical / family emergency inside a hardship statement. This is
# NOT an ordinary "no funds" objection: the recovery ladder pauses for the
# turn and no consequence/offer/borrowing pitch may run.
_MEDICAL_EMERGENCY = re.compile(
    r"hospital|अस्पताल|admit|एडमिट|icu|\bilaa?j\b|इलाज|operation|ऑपरेशन"
    r"|surgery|सर्जरी|bima?ar|beemar|बीमार|बिमार|tabiy?at|तबीयत|तबियत"
    r"|medical|मेडिकल|emergency|इमरजेंसी|accident|एक्सीडेंट|दुर्घटना"
    r"|(?:guzar|गुज़र|गुजर)\s*(?:ga|गए|गया|गयी)|देहांत|निधन|death|expire",
    re.I,
)


def detect_medical_emergency(text: str) -> bool:
    """Medical/family-emergency language inside the caller's statement."""
    stripped = (text or "").strip()
    return bool(stripped) and bool(_MEDICAL_EMERGENCY.search(stripped))


def amount_query_type(text: str) -> str:
    """Which figure was asked for: total/minimum/penalty/overdue/ambiguous."""
    stripped = (text or "").strip()
    for label, pattern in _AMOUNT_TYPE_PATTERNS:
        if pattern.search(stripped):
            return label
    return "ambiguous"
_RECORDING_MENTION = re.compile(r"record|रिकॉर्ड", re.I)
# Identity confirmation is deliberately STRICT. The old permissive matcher
# ("बोल रहा" anywhere counted as yes) confirmed identity from ambiguous or
# corrupted STT like "I mean बोल रहा हूँ।" — after which the LLM happily told
# the caller who they were. A confirmation now requires an ANCHORED clear
# yes: a leading affirmation token, or a first-person "मैं (ही) बोल रहा हूँ"/
# "yes speaking"/"this is <name>" construction. Anything else stays
# unconfirmed and earns a polite re-ask, never a guess.
_IDENTITY_AFFIRM_CLEAR = re.compile(
    # Leading affirmation token ("हाँ...", "जी हाँ...", "yes ..."). NOTE:
    # Python's \b misfires around Devanagari, so the Devanagari alternates
    # end on an explicit not-another-Devanagari-letter lookahead instead.
    r"^\W*(?:haan(?:\s*ji)?|han(?:\s*ji)?|hanji|ji(?:\s*haan|\s*han)?|yes|yeah"
    r"|correct|sahi|bilkul|barabar|barobar)\b"
    r"|^\W*(?:हाँ|हां|जी(?:\s*(?:हाँ|हां))?|सही|बिल्कुल|बराबर)(?![ऀ-ॿ])"
    # First-person speaking: "मैं (ही) बोल रहा हूँ", "main bol raha hoon".
    r"|^\W*(?:main|mai)\b\s*(?:hi\s+)?(?:bol|hoon|hu\b|speaking)"
    r"|^\W*मैं\s*(?:ही\s*)?(?:बोल|हूँ|हूं)"
    r"|\bmain\s+hi\s+(?:hoon|hu|bol)|मैं\s*ही\s*(?:हूँ|हूं|बोल)"
    # English: "yes speaking", "speaking", "this is Devendra", "it's me".
    r"|\bthis is\b|\bit'?s me\b|^\W*speaking\W*$",
    re.I,
)
# Anything carrying a negation can never confirm ("जी नहीं", "no, not me").
_IDENTITY_NEGATION = re.compile(
    r"\b(?:nahi|nahin|no|not|nope)\b|नहीं|नही|गलत|\bgalat\b", re.I
)
# Explicit ambiguity/confusion markers: a reply built around these is a
# mis-heard or partial utterance, not an answer ("I mean ...", "मतलब?",
# "hello?", "कौन बोल रहा है?", bare "बोलिए").
_IDENTITY_AMBIGUOUS = re.compile(
    r"\bi mean\b|\bmatlab\b|मतलब|\bhello+\b|\bhaanlo\b|हेलो|हैलो"
    r"|\bkaun\b|कौन|\bkya\b\W*$|^\W*क्या\W*$"
    r"|^\W*(?:boliye|bolo|bataiye)\W*$|^\W*(?:बोलिए|बोलो|बताइए)\W*$",
    re.I,
)

def identity_answer_from_decision(decision: ConversationDecision) -> str | None:
    """Map a validated Goal Engine decision onto confirm/deny/unclear.

    This is the PRIMARY identity mechanism: the semantic judgement (negation
    over affirmation, ambiguity, noise) was made by the decision layer under
    the bot's policy and validated by the schema. Returns None when the
    decision does not answer the identity question at all (``unrelated`` or
    no gate outcome) — the turn then falls through to its other handling.
    """
    if decision.decision is None or decision.decision == "unrelated":
        return None
    return {
        "confirmed": "confirm",
        "denied": "deny",
        "ambiguous": "unclear",
        "needs_clarification": "unclear",
    }.get(decision.decision, "unclear")


def classify_identity_answer(text: str, signal: str | None) -> str:
    """FALLBACK identity classification — regex, used only when no validated
    decision is available for the turn (LLM down/timeout/unparseable).

    One user turn answering the identity question → confirm/deny/unclear.
    ``deny`` needs explicit evidence (refusal/wrong-person signal, a name
    mismatch, or a plain negation). ``confirm`` needs an anchored clear yes
    with no negation and no ambiguity marker. EVERYTHING else — partial STT,
    background speech, "hello?", "बोलिए", "I mean बोल रहा हूँ" — is
    ``unclear`` and must be re-asked, never assumed.
    """
    stripped = (text or "").strip()
    if not stripped:
        return "unclear"
    if signal in ("refusal", "wrong_person") or _NAME_MISMATCH.search(stripped):
        return "deny"
    if _IDENTITY_NEGATION.search(stripped):
        return "deny" if len(stripped.split()) <= 4 else "unclear"
    if _IDENTITY_AMBIGUOUS.search(stripped):
        return "unclear"
    if _IDENTITY_AFFIRM_CLEAR.search(stripped):
        return "confirm"
    if signal == "affirm" and len(stripped.split()) <= 4:
        # The classifier is confident it's a yes AND the utterance is short
        # enough that nothing else can be hiding in it.
        return "confirm"
    return "unclear"


# ── transaction-reference capture ────────────────────────────────────────────
# 6–22 digits (UTR is typically 12), optionally with a short alpha prefix or
# suffix (bank reference formats). Digit groups the caller read out with
# pauses arrive as space/hyphen-separated groups and are joined first.
_REFERENCE_TOKEN = re.compile(r"[A-Za-z]{0,6}\d{6,22}[A-Za-z0-9]{0,6}")


def normalize_reference_text(text: str) -> str:
    """Digit-normalize an utterance for reference extraction.

    The shared spoken-number layer handles English/Hindi/Hinglish digit
    words, "double"/"triple" repetitions, Devanagari digits, and compound
    Hindi values ("नौ सौ छत्तीस" → 936); joined digit groups then form one
    candidate token. The RAW transcript is never rewritten — this derived
    form exists only for extraction.
    """
    normalized = verbalized_digits(text or "")
    # "1234 5678 9012" / "1234-5678-9012" → "123456789012"
    return re.sub(r"(?<=\d)[\s\-.](?=\d)", "", normalized)


def extract_transaction_reference(text: str) -> str | None:
    """The transaction/UTR reference in an utterance, normalized, or None."""
    normalized = normalize_reference_text(text)
    best: str | None = None
    for match in _REFERENCE_TOKEN.finditer(normalized):
        token = match.group(0)
        if is_valid_transaction_reference(token) and (
            best is None or len(token) > len(best)
        ):
            best = token
    return best.upper() if best else None


def is_valid_transaction_reference(reference: str | None) -> bool:
    """Format check: 6–22 digits, alphanumeric, no separators."""
    if not reference:
        return False
    token = reference.strip()
    if not token.isalnum() or len(token) > 28:
        return False
    digits = sum(ch.isdigit() for ch in token)
    return 6 <= digits <= 22


def spoken_reference(reference: str) -> str:
    """Reference for read-back: digit by digit, so the TTS never garbles it."""
    return " ".join(reference)


# A concrete rupee amount inside a commitment ("₹2000", "do hazaar rupaye").
_AMOUNT_TOKEN = re.compile(
    r"₹?\s*(\d{2,7})(?:\s*(?:rupees|rupay\w*|रुपये|रुपए|rs))?", re.I
)


def _proposed_amount(text: str) -> str | None:
    """The amount the caller proposed (a claim, never a verified figure)."""
    normalized = verbalized_digits(text or "").replace(",", "")
    match = _AMOUNT_TOKEN.search(normalized)
    return match.group(1) if match else None


# Dispositions, most-significant-first (index = priority).
_DISPOSITION_PRIORITY = (
    "wrong_number",
    "identity_mismatch",
    "identity_unverified",
    "account_disputed",
    "payment_claimed",
    "complaint_recorded",
    "escalated",
    "callback_requested",
    "promise_to_pay",
    "payment_initiated",
    "hardship",
    "refused_to_pay",
    "no_commitment",
)


@dataclass
class TurnPlan:
    """What the brain must do with the current user turn."""

    force_llm: bool = False          # answer with the LLM; do NOT advance the workflow
    handoff: bool = False            # deterministic transfer to a human agent
    close_after_reply: bool = False  # this reply is the goodbye; end the call after it
    instruction: str = ""            # per-turn system-prompt block
    # Fully determined reply to speak WITHOUT an LLM round trip. Set only when
    # the turn's content follows from verified facts alone; empty otherwise,
    # and the LLM answers as usual.
    scripted_reply: str = ""
    # The scripted reply must be spoken EVEN IF a tool ran this turn — set for
    # replies that already encode the tool's verified outcome (payment
    # verification results) or that must never be left to the LLM's judgement
    # (identity re-asks, transaction-number asks).
    scripted_final: bool = False
    # The policy-selected action for this turn (auditable; always a member of
    # allowed_actions() for the current state).
    action: str = ""
    # A captured transaction reference the brain must verify with the
    # configured payment tool BEFORE replying. The policy is re-planned after
    # record_payment_verification folds the result in.
    verify_reference: str | None = None
    # The caller asked for an account figure and an account-status tool is
    # configured: the brain must run the REAL lookup before replying (with a
    # natural pre-speech acknowledgment), then re-plan on the fresh values.
    refresh_account: bool = False


@dataclass
class CollectionCallPolicy:
    context: CustomerContextSnapshot | None = None
    language: str = "hi-IN"
    # Whether a backend payment-status tool is configured for this bot. It
    # flips the prompt from "you cannot check anything on this call" to
    # "state only what the tool verified".
    tools_available: bool = False

    phase: str = GREETING
    verified: bool = False
    awaiting_identity: bool = False
    recording_notice_given: bool = False

    wrong_party: bool = False
    identity_mismatch: bool = False
    # How many identity answers were too unclear to act on (re-asked each
    # time, up to _MAX_IDENTITY_REASKS, then the call closes unverified).
    identity_unclear_count: int = 0
    dispute_raised: bool = False
    payment_claimed: bool = False
    payment_claim_stage: int = 0  # 0 none, 1 reference pending, 2 resolved/closed
    # Transaction-reference capture state. "The customer HAS a number",
    # "the customer is SAYING the number", "the number was CAPTURED" and
    # "the payment was VERIFIED" are four different facts — none implies the
    # next, and only a captured value may ever be called noted/recorded.
    awaiting_reference: bool = False
    transaction_reference: str | None = None
    reference_attempts: int = 0
    reference_unavailable: bool = False
    payment_method_claimed: str | None = None
    payment_date_claimed: str | None = None
    # Result of the payment-status tool for THIS call: None = never checked
    # (no tool / tool failed), otherwise the PAYMENT_STATUSES value the
    # backend returned. An account is marked paid ONLY from this — never
    # from the claim itself (regex or LLM output alone must not settle it).
    payment_verified_status: str | None = None
    # Normalized verification outcome once a REAL check ran for the claim:
    # verified | pending | failed | unverified (no tool available / tool
    # errored — verification stays honestly pending). None = not checked yet.
    verification_outcome: str | None = None
    verification_source: str | None = None
    complaint_raised: bool = False
    callback_requested: bool = False
    callback_time_known: bool = False
    hardship_raised: bool = False
    refusals: int = 0
    promise_to_pay: bool = False
    payment_initiated: bool = False
    escalated: bool = False
    interruption_detected: bool = False
    # ── recovery-ladder state (owned HERE, not by prompt history-scanning:
    # two independent trackers of the same rungs is how repeated pitches
    # happened). A rung is marked used when its instruction is issued.
    consequence_used: bool = False
    offer_used: bool = False
    partial_used: bool = False
    self_resolution_used: bool = False
    final_options_offered: bool = False
    # Payment-commitment slots observed so far (claims, never verified facts):
    # what amount the caller proposed and whether a concrete date was given.
    proposed_amount: str | None = None
    promise_date_known: bool = False
    # THIS turn's amount question ("total"/"overdue"/"minimum"/"penalty"/
    # "ambiguous"); None when the latest turn asked no amount question.
    pending_amount_type: str | None = None
    # Serious-hardship handling: medical/family emergency pauses the ladder
    # for the turn; the acknowledgment is made once, not per mention.
    hardship_acknowledged: bool = False
    # Whether an account-status/amount tool is configured for this bot,
    # whether it already ran this call, and whether it actually SUCCEEDED
    # (only a successful refresh may be described as fresh figures).
    account_tool_available: bool = False
    account_refreshed: bool = False
    account_refresh_succeeded: bool = False
    # How the LAST observed turn was interpreted: "decision" (validated Goal
    # Engine output) or "regex" (deterministic fallback). Observability only.
    last_interpretation_source: str = "regex"

    claims: list[str] = field(default_factory=list)  # customer statements (unverified)
    _last_bot_reply: str = ""
    _bot_offered_agent: bool = False
    # Identity was confirmed and the bot has not replied since. Cleared by
    # observe_bot (not by plan_turn): a late-final merge cancels the reply
    # and re-plans the SAME turn, and the re-run must still be claimed by
    # the policy instead of falling through to the scripted ladder.
    _just_verified: bool = False
    # THIS turn's identity answer was unclear (set by observe_user, consumed
    # by plan_turn to script the re-ask instead of freeing the LLM).
    _identity_unclear_turn: bool = False
    # THIS turn raised hardship / a medical emergency (turn-scoped: drives
    # the empathy-first handling exactly once, on the turn it was said).
    _hardship_turn: bool = False
    _medical_turn: bool = False
    # The ladder rung selected for THIS turn's reply (turn-scoped).
    _ladder_rung_turn: str | None = None
    # THIS turn captured the transaction reference (drives read-back).
    _reference_captured_turn: bool = False
    _closed: bool = False
    started_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if self.context is not None:
            # Identity is confirmed per CALL, never inherited: a stored
            # customer_verified=True only means a previous call verified them —
            # whoever answers THIS call must still confirm before any account
            # detail is disclosed.
            self.dispute_raised = bool(self.context.account_disputed)
            self.complaint_raised = bool(self.context.complaint_pending)
            self.payment_claimed = self.context.payment_status == "completed"
            self.recording_notice_given = not self.context.recording_notice_required
            if self.context.preferred_language:
                self.language = self.context.preferred_language
        if not self.verified:
            self.phase = IDENTITY_VERIFICATION

    # ── observations ─────────────────────────────────────────────────────

    def observe_bot(self, text: str) -> None:
        """Track what the bot just said (identity question, agent offer,
        recording notice) so short answers land on the right question."""
        self._last_bot_reply = text or ""
        self._just_verified = False  # the bot has now responded to the verification
        self._bot_offered_agent = bool(_AGENT_OFFER.search(self._last_bot_reply))
        if _IDENTITY_QUESTION.search(self._last_bot_reply) and not self.verified:
            self.awaiting_identity = True
        if _RECORDING_MENTION.search(self._last_bot_reply):
            self.recording_notice_given = True

    def observe_user(
        self,
        text: str,
        signal: str | None,
        decision: ConversationDecision | None = None,
    ) -> None:
        """Fold one user turn into the call state (before routing/replying).

        ``decision`` is the Goal Engine's validated interpretation of the
        turn; when present it is the PRIMARY source of meaning (identity
        outcome, slot observations). Without one — the LLM failed or the
        engine is disabled — the legacy deterministic patterns take over so
        a provider outage degrades understanding, never drops turns.
        """
        stripped = (text or "").strip()
        self._identity_unclear_turn = False
        self._reference_captured_turn = False
        self._hardship_turn = False
        self._medical_turn = False
        self._ladder_rung_turn = None
        self.pending_amount_type = None
        self.last_interpretation_source = "regex" if decision is None else "decision"
        if not stripped:
            return

        claim: str | None = None

        # A pending transaction-reference question consumes this turn FIRST:
        # the next utterance after "कृपया ट्रांजैक्शन नंबर बताइए" is parsed
        # specifically as the reference (see _observe_reference_answer).
        if self.awaiting_reference and self.transaction_reference is None:
            if self._observe_reference_answer(stripped, signal, decision):
                return

        # Identity outcome for a pending identity question. Mismatch evidence
        # is checked FIRST — "जी नहीं" contains an affirm token but denies.
        if self.awaiting_identity:
            # The validated decision answers the gate directly (PRIMARY). The
            # regex classifier fills in only when no decision judged the gate
            # at all — and never for off-goal turns, whose gate outcome the
            # schema already neutralized. Turns that carry a DIFFERENT
            # meaning (a question, a complaint, hardship, an agent request,
            # a payment claim, "unrelated" …) are not answers to the identity
            # question — they fall through to their own handling below with
            # identity still unconfirmed.
            answer: str | None = None
            regex_eligible = signal in (
                None, "affirm", "refusal", "wrong_person", "clarify",
            )
            if decision is not None and decision.decision is not None:
                answer = identity_answer_from_decision(decision)
            elif (
                decision is None or decision.scope == SCOPE_IN
            ) and regex_eligible:
                answer = classify_identity_answer(stripped, signal)
            if answer is not None:
                if answer == "deny":
                    self.awaiting_identity = False
                    self.identity_mismatch = True
                    self.wrong_party = True
                    self.phase = WRONG_PARTY
                    claim = stripped
                elif answer == "confirm":
                    self.verified = True
                    self.awaiting_identity = False
                    self._just_verified = True
                    if self.phase == IDENTITY_VERIFICATION:
                        self.phase = ACCOUNT_EXPLANATION
                else:
                    # Unclear / partial / noisy: identity stays UNCONFIRMED
                    # and the turn is claimed by the scripted re-ask.
                    self.identity_unclear_count += 1
                    self._identity_unclear_turn = True
                    return

        if _NAME_MISMATCH.search(stripped) and not self.verified:
            self.identity_mismatch = True
            self.wrong_party = True
            self.phase = WRONG_PARTY
            claim = stripped

        if signal == "wrong_person":
            if _WRONG_NUMBER.search(stripped) or not _DISPUTE.search(stripped):
                self.wrong_party = True
                self.phase = WRONG_PARTY
            else:
                self.dispute_raised = True
                self.phase = ACCOUNT_DISPUTE
            claim = stripped
        elif _DISPUTE.search(stripped):
            self.dispute_raised = True
            self.phase = ACCOUNT_DISPUTE
            claim = stripped

        if signal == "already_paid":
            self.payment_claimed = True
            if self.payment_claim_stage == 0:
                self.payment_claim_stage = 1
            self.phase = PAYMENT_ALREADY_MADE
            claim = stripped
            self._note_payment_details(stripped)
            reference = self._reference_from_decision(decision) \
                or extract_transaction_reference(stripped)
            if reference and self.transaction_reference is None:
                self._capture_reference(reference)
            elif self.transaction_reference is None and self.verification_outcome is None:
                # The claim carries no usable reference: the NEXT step is to
                # ask for the actual transaction number. "हाँ, नंबर है" later
                # is NOT the number — only a captured value closes this.
                self.awaiting_reference = True
        elif (
            self.payment_claimed
            and self.transaction_reference is None
            and self.verification_outcome is None
            and not self.reference_unavailable
        ):
            # Any later turn may still volunteer the reference (or the
            # method/date) without being asked.
            self._note_payment_details(stripped)
            reference = self._reference_from_decision(decision) \
                or extract_transaction_reference(stripped)
            if reference:
                self._capture_reference(reference)
                claim = stripped

        if signal == "complaint":
            self.complaint_raised = True
            if not self.blockers():
                self.phase = COMPLAINT_HANDLING
            claim = stripped

        if signal == "callback":
            self.callback_requested = True
            self.callback_time_known = bool(_TIME_HINT.search(stripped))
            if not self.blockers():
                self.phase = CALLBACK_REQUESTED
            claim = stripped
        elif self.callback_requested and not self.callback_time_known and (
            _TIME_HINT.search(stripped) or signal == "affirm"
        ):
            self.callback_time_known = True
            claim = claim or stripped

        if signal == "hardship":
            self.hardship_raised = True
            self._hardship_turn = True
            self._medical_turn = detect_medical_emergency(stripped)
            claim = stripped
        elif detect_medical_emergency(stripped) and signal in (None, "refusal",
                                                               "callback"):
            # Medical/family-emergency language without the hardship signal
            # (e.g. "मम्मी hospital में हैं, बाद में call करना") still gets
            # the humane handling, never a plain refusal/callback script.
            self.hardship_raised = True
            self._hardship_turn = True
            self._medical_turn = True
            claim = claim or stripped
        if signal == "refusal":
            self.refusals += 1
        if signal == "agent_request":
            self.escalated = True
            self.phase = ESCALATION
        # An amount question is detected on the utterance itself (domain
        # rule): "कितना payment करना है?" routes as an ASK regardless of what
        # the generic signal bank labelled it.
        if detect_amount_query(stripped):
            self.pending_amount_type = amount_query_type(stripped)
        if signal == "payment_intent" and self.pending_amount_type is None \
                and not self.blockers():
            self.promise_to_pay = True
            if self.phase in (ACCOUNT_EXPLANATION, PAYMENT_DISCUSSION, GREETING,
                              IDENTITY_VERIFICATION):
                self.phase = PAYMENT_DISCUSSION
            if _TIME_HINT.search(stripped):
                self.callback_time_known = True
                self.promise_date_known = True
            amount = _proposed_amount(stripped)
            if amount:
                self.proposed_amount = amount
            # The commitment turn may already name the method ("UPI se kar
            # dunga") — recorded as a claim, exactly like the claim flow.
            self._note_payment_details(stripped)

        if claim:
            snippet = claim[:160]
            if snippet not in self.claims:
                self.claims.append(snippet)
                del self.claims[:-8]

    def _observe_reference_answer(
        self,
        stripped: str,
        signal: str | None,
        decision: ConversationDecision | None = None,
    ) -> bool:
        """Parse one turn as the answer to "कृपया ट्रांजैक्शन नंबर बताइए".

        Returns True when the turn was consumed by the reference question.
        Turns carrying a different meaning (complaint, agent request,
        hardship, callback, a real question) are NOT consumed — they fall
        through to their own handling with the reference still awaited.

        The Goal Engine's slot observation is the primary interpretation:
        ``provided`` values are still format-validated deterministically
        before capture, ``exists_claimed``/``unclear`` count as an attempt
        (the value has NOT been said), ``unavailable``/``refused`` close the
        ask honestly. The regexes below are the no-decision fallback.
        """
        if signal in (
            "complaint", "agent_request", "hardship", "callback",
            "wrong_person", "question",
        ):
            return False
        if detect_amount_query(stripped):
            # "कितना बाक़ी है?" while the reference is awaited is an amount
            # question, never an attempt at saying the number.
            return False
        self._note_payment_details(stripped)
        # Semantic-first: what did the decision layer observe for this slot?
        observation = (
            decision.slots.get("transaction_reference")
            if decision is not None else None
        )
        reference = self._reference_from_decision(decision) \
            or extract_transaction_reference(stripped)
        if reference:
            self._capture_reference(reference)
            return True
        if observation is not None and observation.status in ("unavailable", "refused"):
            self.reference_unavailable = True
            self.awaiting_reference = False
            self.payment_claim_stage = 2
            return True
        if observation is not None:
            # exists_claimed / unclear / provided-but-format-invalid: saying
            # the number exists is not the number — ask for the value itself.
            self.reference_attempts += 1
            return True
        if signal == "refusal" or _NO_REFERENCE.search(stripped):
            # They cannot provide it: that is recorded honestly; the claim is
            # noted for the team, never called verified.
            self.reference_unavailable = True
            self.awaiting_reference = False
            self.payment_claim_stage = 2
            return True
        # "हाँ, नंबर है" / partial STT / anything without an actual value:
        # the number has NOT been provided — ask for the value itself.
        self.reference_attempts += 1
        return True

    @staticmethod
    def _reference_from_decision(
        decision: ConversationDecision | None,
    ) -> str | None:
        """A decision-provided transaction reference, format-validated.

        The decision layer says the caller PROVIDED a value; capture still
        requires the value to normalize into a valid reference — semantic
        interpretation never bypasses deterministic schema validation.
        """
        if decision is None:
            return None
        observation = decision.slots.get("transaction_reference")
        if observation is None or observation.status != "provided":
            return None
        return extract_transaction_reference(observation.value or "")

    def _capture_reference(self, reference: str) -> None:
        self.transaction_reference = reference
        self.awaiting_reference = False
        self._reference_captured_turn = True
        if self.payment_claim_stage == 0:
            self.payment_claim_stage = 1

    def _note_payment_details(self, stripped: str) -> None:
        """Record the claimed method/date (as claims, never as facts)."""
        if self.payment_method_claimed is None:
            match = _PAYMENT_METHOD.search(stripped)
            if match:
                token = match.group(0)
                for pattern, canonical in _METHOD_CANONICAL:
                    if pattern.search(token):
                        self.payment_method_claimed = canonical
                        break
        if self.payment_date_claimed is None:
            match = _PAYMENT_DATE.search(stripped)
            if match:
                self.payment_date_claimed = match.group(0)

    _VERIFIED_STATUSES = frozenset({
        "completed", "success", "succeeded", "verified", "paid", "captured",
        "confirmed", "settled",
    })
    _PENDING_STATUSES = frozenset({
        "processing", "pending", "initiated", "in_progress", "created",
    })
    _FAILED_STATUSES = frozenset({
        "failed", "failure", "not_found", "no_match", "declined", "rejected",
        "reversed", "expired",
    })

    def record_payment_verification(
        self, status: str | None, source: str | None = None,
        *, for_reference: bool = False,
    ) -> None:
        """Fold the payment-status TOOL result into the call state.

        Called by the brain after a real check ran (the account-level
        payment-status tool on the claim turn, or the reference verification
        once the transaction number was captured). A positive confirmation —
        or an honest "still processing" — resolves the claim from the
        account-level check; once a reference is in hand every answer —
        including "no tool available" (``status=None`` with
        ``for_reference=True``) — produces an honest outcome: verified /
        pending / failed / unverified. A None/failed check before any
        reference exists changes nothing (the UTR ask is the fallback).
        """
        if source:
            self.verification_source = source
        normalized = (
            str(status).strip().lower().replace(" ", "_") if status else None
        )
        if normalized:
            self.payment_verified_status = normalized
        if normalized in self._VERIFIED_STATUSES:
            self.verification_outcome = "verified"
            self.awaiting_reference = False
            self.payment_claim_stage = 2
            return
        if not (for_reference or self.transaction_reference):
            # Account-level check without a reference: only a positive
            # confirmation resolves. A pending/failed status is surfaced
            # honestly via the Backend-verification prompt block while the
            # UTR is still collected as evidence for the follow-up team.
            return
        if normalized in self._PENDING_STATUSES:
            self.verification_outcome = "pending"
        elif normalized in self._FAILED_STATUSES:
            self.verification_outcome = "failed"
        elif normalized is None:
            self.verification_outcome = "unverified"
        else:
            # An answer we do not recognize is treated as still-processing —
            # never as verified.
            self.verification_outcome = "pending"
        self.awaiting_reference = False
        self.payment_claim_stage = 2

    # Canonical account-figure fields an account-status tool may refresh,
    # with the wire-name aliases tenants commonly use.
    _ACCOUNT_FIELD_ALIASES = {
        "overdue_amount": ("overdue_amount", "overdueamount", "overdue"),
        "total_outstanding": (
            "total_outstanding", "totaloutstanding", "total", "outstanding",
            "outstanding_amount",
        ),
        "minimum_payable": (
            "minimum_payable", "minimumpayable", "minimum", "min_due",
            "minimum_due", "min_amount",
        ),
        "penal_charges": (
            "penal_charges", "penalcharges", "penalty", "late_fee",
            "latefee", "penalty_amount",
        ),
        "due_date": ("due_date", "duedate"),
        "days_overdue": ("days_overdue", "daysoverdue", "dpd"),
    }

    def record_account_refresh(self, payload: dict | None, source: str | None = None) -> None:
        """Fold a REAL account-status tool result into the live facts.

        Only recognized figure fields update; everything else in the payload
        is ignored here (the brain separately surfaces the raw tool result to
        the LLM). A failed/empty lookup marks the refresh attempted so the
        turn does not retry in a loop — the reply then uses the loaded facts
        and never claims a fresh check succeeded.
        """
        self.account_refreshed = True
        if not payload or self.context is None:
            return
        lowered = {
            str(key).strip().lower().replace(" ", "_"): value
            for key, value in payload.items()
        }
        updates: dict = {}
        for field_name, aliases in self._ACCOUNT_FIELD_ALIASES.items():
            for alias in aliases:
                if alias not in lowered or lowered[alias] is None:
                    continue
                value = lowered[alias]
                try:
                    if field_name == "due_date":
                        updates[field_name] = str(value)
                    elif field_name == "days_overdue":
                        updates[field_name] = int(float(value))
                    else:
                        updates[field_name] = float(value)
                except (TypeError, ValueError):
                    pass
                else:
                    break
        if updates:
            # A successful HTTP/tool result is not automatically a successful
            # FIGURE refresh.  If it returned only unrelated fields (for
            # example {"status": "ok"}), keep the loaded snapshot and never
            # tell the caller that the amount was freshly verified.
            self.account_refresh_succeeded = True
            # The snapshot is frozen (verified facts are never mutated in
            # place) — refreshed figures produce a NEW snapshot.
            self.context = dataclass_replace(self.context, **updates)

    # ── decisions ────────────────────────────────────────────────────────

    def preempts_turn(self, text: str) -> bool:
        """Whether the policy will consume this turn deterministically.

        FALLBACK-PATH ONLY: when the Goal Engine is disabled or failed for
        the turn, the brain uses this to skip the legacy LLM classification
        hop on turns whose fallback handling cannot depend on it: the answer
        to a pending transaction-number question, and identity-question
        answers the deterministic rules already resolve (clear yes / clear
        no / unclear → scripted re-ask). On the agentic path identity and
        slot answers are decided semantically, never preempted by regex. A
        turn whose regex signal carries OTHER meaning (complaint, agent
        request, hardship, …) still classifies normally.
        """
        from shared.orchestration.router import classify_user_signal

        stripped = (text or "").strip()
        if not stripped:
            return False
        signal = classify_user_signal(stripped)
        if (
            self.awaiting_reference
            and self.transaction_reference is None
            and self.verification_outcome is None
        ):
            return signal not in (
                "complaint", "agent_request", "hardship", "callback",
                "wrong_person", "question",
            )
        if self.awaiting_identity and not self.verified:
            return signal in (
                None, "affirm", "refusal", "wrong_person", "clarify",
            )
        return False

    # Fast-path clarity gates: only SHORT, single-thought utterances resolve
    # deterministically. Anything longer may be compound ("haan, lekin …") and
    # goes to the Goal Engine, whose semantic judgement these limits protect.
    _FAST_PATH_MAX_WORDS = 8
    _FAST_PATH_AGENT_MAX_WORDS = 6
    # The payment_intent signal also fires on a bare method mention ("UPI se
    # ho jayega kya?"), which is a QUESTION, not a commitment. The fast path
    # additionally requires an explicit commitment verb and no interrogative.
    _CLEAR_COMMITMENT = re.compile(
        r"karunga|karungi|करूंगा|करूंगी"
        r"|(?:kar|कर|de|दे|bhar|भर)\s*(?:dunga|dungi|deta|deti|दूंगा|दूंगी|देता|देती)"
        r"|i (?:will|can) pay|ready to pay|taiyar|तैयार",
        re.I,
    )
    _INTERROGATIVE = re.compile(
        r"\b(?:kya|kab|kaise|kyun|kyon|kahan|kitn\w*|what|when|how|why|where)\b"
        r"|क्या|कब|कैसे|क्यों|कहाँ|कहां|कितन",
        re.I,
    )

    def deterministic_turn_resolution(self, text: str, signal: str | None) -> str | None:
        """Name of the deterministic rule that fully resolves this turn, or None.

        Used by the brain BEFORE the Goal Engine: a turn the policy/parser can
        resolve with high confidence (a clear yes/no to the pending identity
        question, an explicit transaction reference for the pending slot, an
        accepted agent offer, a short explicit agent request, a clear payment
        commitment/refusal while the recovery discussion waits for one) never
        pays the decision-LLM latency. Everything compound, contradictory,
        ambiguous or off-question returns None and is judged semantically.

        The rules deliberately reuse the same deterministic machinery the
        fallback path runs on (classify_identity_answer, reference
        extraction, the signal bank) gated by the CURRENT pending question —
        never a broad regex on its own.
        """
        stripped = (text or "").strip()
        if not stripped:
            return None
        words = len(stripped.split())

        # Pending transaction-reference question: only an ACTUAL value (which
        # deterministic format validation accepts) or an explicit can't-provide
        # resolves without the engine. "हाँ, नंबर है" and everything else stays
        # semantic — the engine distinguishes exists-claims from noise.
        if (
            self.awaiting_reference
            and self.transaction_reference is None
            and self.verification_outcome is None
        ):
            if signal in (
                "complaint", "agent_request", "hardship", "callback",
                "wrong_person", "question",
            ):
                return None
            if extract_transaction_reference(stripped):
                return "reference_provided"
            if signal == "refusal" or _NO_REFERENCE.search(stripped):
                return "reference_unavailable"
            return None

        # Pending identity question: a clear anchored yes (no negation, no
        # ambiguity marker) or an explicit denial resolves deterministically.
        # Unclear/partial answers go to the engine — the scripted re-ask is
        # only their FALLBACK, not their fast path.
        if self.awaiting_identity and not self.verified:
            if words > self._FAST_PATH_MAX_WORDS or "?" in stripped:
                return None
            answer = classify_identity_answer(stripped, signal)
            if answer == "confirm" and signal in (None, "affirm"):
                return "identity_confirmed"
            if answer == "deny" and signal in ("refusal", "wrong_person"):
                return "identity_denied"
            return None

        # Yes to the bot's own "shall I connect you to an agent?" — the affirm
        # signal is whole-utterance anchored, so it cannot hide a second ask.
        if self._bot_offered_agent and signal == "affirm":
            return "agent_offer_accepted"

        # Explicit human-agent request. Kept to short utterances: the signal
        # pattern alone matches any mention of "agent", which is not a request.
        if signal == "agent_request" and words <= self._FAST_PATH_AGENT_MAX_WORDS:
            return "agent_requested"

        # Clear payment willingness/unwillingness or inability while the recovery
        # discussion is waiting for exactly that answer. Identity must be
        # confirmed and nothing else open — any blocker means the reply is a
        # judgement call.
        if (
            signal in ("payment_intent", "refusal", "hardship")
            and self.verified
            and not self.wrong_party
            and not self.blockers()
            and words <= self._FAST_PATH_MAX_WORDS
            and "?" not in stripped
            and not self._INTERROGATIVE.search(stripped)
        ):
            if signal == "refusal":
                return "payment_refusal"
            if signal == "hardship":
                return "payment_hardship"
            if self._CLEAR_COMMITMENT.search(stripped):
                return "payment_commitment"
        return None

    def _advance_ladder(self) -> str:
        """The next unused recovery rung; marks it used.

        consequence → offer (only when a verified offer exists) → partial
        (only when partial payment is allowed) → self-resolution → final
        options (callback/agent) → closed. Unavailable rungs are skipped, a
        used rung never repeats, and this state is the ONLY ladder tracker —
        the prompt no longer scans history for what was already pitched.
        """
        ctx = self.context
        if not self.consequence_used:
            self.consequence_used = True
            self._ladder_rung_turn = "consequence"
            return "consequence"
        if not self.offer_used and ctx is not None and (
            ctx.active_offers or ctx.offer_terms
        ):
            self.offer_used = True
            self._ladder_rung_turn = "offer"
            return "offer"
        if not self.partial_used and ctx is not None \
                and ctx.partial_payment_allowed:
            self.partial_used = True
            self._ladder_rung_turn = "partial"
            return "partial"
        if not self.self_resolution_used:
            self.self_resolution_used = True
            self._ladder_rung_turn = "self_resolution"
            return "self_resolution"
        if not self.final_options_offered:
            self.final_options_offered = True
            self._ladder_rung_turn = "final_options"
            return "final_options"
        self._ladder_rung_turn = "closed"
        return "closed"

    def blockers(self) -> list[str]:
        open_blockers: list[str] = []
        if self.wrong_party:
            open_blockers.append("wrong party / identity mismatch")
        if self.dispute_raised:
            open_blockers.append("account disputed by customer")
        if self.payment_claimed and self.payment_claim_stage < 2:
            open_blockers.append("customer says payment already made (unverified)")
        if self.complaint_raised:
            open_blockers.append("complaint raised")
        return open_blockers

    def plan_turn(
        self, text: str, signal: str | None, *, workflow_active: bool = False,
    ) -> TurnPlan:
        """Decide how the brain must handle this turn. Call AFTER observe_user.

        ``workflow_active`` — a tenant-authored workflow currently owns the
        conversation flow: the policy then never claims amount/commitment/
        ladder turns for itself (the workflow's own nodes and its off-script
        LLM fallback receive this policy's per-turn instruction instead).
        """
        plan = TurnPlan()
        just_verified = self._just_verified

        # An affirmative to the bot's own "shall I connect you to an agent?"
        # must transfer — not fall back onto a stale workflow question.
        if self._bot_offered_agent and signal == "affirm":
            self.escalated = True
            self.phase = ESCALATION
            plan.handoff = True
            return plan
        if signal == "agent_request":
            plan.handoff = True
            return plan

        if self._identity_unclear_turn and not self.verified:
            # The identity answer was ambiguous, partial or noise. The reply
            # is fully determined and NEVER left to the LLM (which is exactly
            # how "जी हाँ, मैं Devendra जी से ही बात कर रहा हूँ … आपकी मदद
            # कैसे कर सकता हूँ?" happened): politely re-ask, up to the limit,
            # then close without verification — and without any disclosure.
            name = (self.context.customer_name if self.context else "") or ""
            if self.identity_unclear_count > _MAX_IDENTITY_REASKS:
                plan.action = "close_unverified"
                plan.scripted_reply = canned(
                    "collections_identity_unverified_close", self.language
                )
                plan.scripted_final = True
                plan.close_after_reply = True
                self.phase = CLOSING
            elif name:
                plan.action = "ask_identity_confirmation"
                plan.scripted_reply = canned(
                    "collections_identity_reask", self.language
                ).format(name=name)
                plan.scripted_final = True
            else:
                # No customer record to name — the LLM re-asks generically
                # under the unconfirmed-identity instruction.
                plan.action = "ask_identity_confirmation"
                plan.force_llm = True
            plan.instruction = self.turn_instruction()
            return plan
        if self.wrong_party:
            # One respectful close: no account details, confirm the number
            # will be flagged for verification, goodbye.
            plan.action = "wrong_person_close"
            plan.force_llm = True
            plan.close_after_reply = True
            self.phase = CLOSING
        elif self.dispute_raised:
            plan.force_llm = True
            plan.action = "record_dispute"
            # Dispute recorded → offer verification callback or agent; close
            # once they answered that one question.  A refusal which RAISES
            # the dispute ("I never took this loan") cannot also answer an
            # offer the bot has not spoken yet; wait until the previous bot
            # reply actually contained the agent/callback choice.
            # An affirmative to that offer already took the deterministic
            # handoff return at the top of this method.
            if (
                self._bot_offered_agent
                and signal in ("refusal", "callback")
                and self.phase in (ACCOUNT_DISPUTE, CLOSING)
            ):
                plan.close_after_reply = True
                self.phase = CLOSING
        elif self.pending_amount_type is not None and self.verified \
                and not workflow_active:
            # Answer-first: the caller asked HOW MUCH. This outranks every
            # script step (including a pending reference ask and the recovery
            # ladder) — the reply states the requested labelled figure, from
            # a REAL lookup when an account tool is configured.
            plan.action = "answer_amount_question"
            plan.force_llm = True
            if self.account_tool_available and not self.account_refreshed:
                plan.refresh_account = True
        elif self.payment_claimed:
            self._plan_payment_claim(plan)
        elif self.complaint_raised:
            plan.force_llm = True
        elif self.callback_requested and not self._medical_turn:
            plan.force_llm = True
            if self.callback_time_known:
                plan.close_after_reply = True
                self.phase = CLOSING
        elif self._medical_turn and not workflow_active:
            # A genuine medical/family emergency is NOT an ordinary refusal:
            # the ladder pauses for this turn — no consequence, offer,
            # partial pitch or borrowing suggestion may run.
            plan.action = "acknowledge_hardship"
            plan.force_llm = True
        elif self.promise_to_pay and signal in ("payment_intent", "affirm") \
                and not workflow_active:
            # The caller has AGREED to pay (now or on this turn): recovery
            # pitches are over. Move to the next missing commitment element
            # (date → method → confirm), never a discount/CIBIL/partial line.
            plan.action = "confirm_commitment"
            plan.force_llm = True
            if self.promise_date_known and self.payment_method_claimed:
                plan.close_after_reply = True
                self.phase = CLOSING
        elif signal in ("question", "clarify", "complaint"):
            # Answer what the customer actually asked before any script step.
            plan.force_llm = True
        elif signal in ("refusal", "hardship") and self.verified \
                and not self.wrong_party and not workflow_active:
            # The recovery ladder runs ONLY here: a genuine payment refusal /
            # inability with nothing higher-priority open and no workflow
            # owning the flow. Each rung is used exactly once per call,
            # tracked in code — never re-derived from scanning history.
            plan.action = f"recovery_{self._advance_ladder()}"
            plan.force_llm = True
            if plan.action == "recovery_closed":
                plan.close_after_reply = True
                self.phase = CLOSING
        elif just_verified:
            # The turn that ANSWERED the identity question must not feed the
            # scripted ladder as if it answered a payment pitch — the LLM
            # opens the account explanation from the now-unlocked facts.
            plan.force_llm = True
            # ...unless the opener is fully determined, in which case speaking
            # it directly removes ~1s of LLM time from the turn where the
            # caller has just said one word and is waiting.
            plan.scripted_reply = self._scripted_opening()
        elif not self.verified and self.context is not None:
            # No account specifics may be pushed before identity confirmation.
            plan.force_llm = True

        plan.instruction = self.turn_instruction()
        return plan

    def _plan_payment_claim(self, plan: TurnPlan) -> None:
        """Drive the payment-already-made flow one validated step at a time.

        Has a number → is saying the number → number captured → verification
        ran → outcome spoken. Each is a separate state; nothing skips ahead,
        and every outcome reply is scripted from the TOOL result — the LLM
        never gets to declare a payment verified or "details recorded".
        """
        plan.force_llm = True
        reference = self.transaction_reference or ""
        spoken = spoken_reference(reference)
        outcome = self.verification_outcome
        if outcome == "verified":
            plan.action = "mark_payment_verified"
            plan.scripted_reply = canned(
                "collections_payment_verified", self.language
            )
            plan.scripted_final = True
            plan.close_after_reply = True
            self.phase = CLOSING
        elif outcome == "pending":
            plan.action = "mark_payment_details_recorded"
            plan.scripted_reply = canned(
                "collections_payment_processing", self.language
            ).format(reference=spoken)
            plan.scripted_final = True
            plan.close_after_reply = True
            self.phase = CLOSING
        elif outcome == "failed":
            plan.action = "schedule_follow_up"
            plan.scripted_reply = canned(
                "collections_payment_not_found", self.language
            ).format(reference=spoken)
            plan.scripted_final = True
            plan.close_after_reply = True
            self.phase = CLOSING
        elif outcome == "unverified":
            # No real verification tool could run: verification stays
            # honestly PENDING — never claimed verified, never "account will
            # be updated".
            plan.action = "mark_payment_details_recorded"
            plan.scripted_reply = canned(
                "collections_verification_unavailable", self.language
            ).format(reference=spoken)
            plan.scripted_final = True
            plan.close_after_reply = True
            self.phase = CLOSING
        elif self.transaction_reference:
            # Captured this turn (or volunteered earlier): verify NOW with
            # the configured tool before any reply is produced.
            plan.action = "verify_payment"
            plan.verify_reference = self.transaction_reference
        elif self.reference_unavailable:
            plan.action = "record_claim_for_follow_up"
            plan.scripted_reply = canned(
                "collections_reference_unavailable_close", self.language
            )
            plan.scripted_final = True
            plan.close_after_reply = True
            self.payment_claim_stage = 2
            self.phase = CLOSING
        elif self.awaiting_reference:
            if self.reference_attempts > _MAX_REFERENCE_ATTEMPTS:
                # Asked, clarified, still nothing usable: close honestly with
                # the claim recorded for the team (spec completion outcome 4).
                plan.action = "record_claim_for_follow_up"
                plan.scripted_reply = canned(
                    "collections_reference_unavailable_close", self.language
                )
                plan.scripted_final = True
                plan.close_after_reply = True
                self.reference_unavailable = True
                self.payment_claim_stage = 2
                self.phase = CLOSING
            elif self.reference_attempts > 0:
                plan.action = "clarify_transaction_reference"
                plan.scripted_reply = canned(
                    "collections_ask_reference_retry", self.language
                )
                plan.scripted_final = True
            else:
                plan.action = "ask_transaction_reference"
                plan.scripted_reply = canned(
                    "collections_ask_reference", self.language
                )
                plan.scripted_final = True
        # else: a context-carried claim with nothing pending this turn — the
        # LLM answers under the live-state instruction (next step asks for
        # the payment details).

    def disposition(self) -> str:
        flags = {
            "wrong_number": self.wrong_party and not self.identity_mismatch,
            "identity_mismatch": self.identity_mismatch,
            "identity_unverified": (
                not self.verified and not self.wrong_party
                and self.identity_unclear_count > _MAX_IDENTITY_REASKS
            ),
            "account_disputed": self.dispute_raised,
            "payment_claimed": self.payment_claimed,
            "complaint_recorded": self.complaint_raised,
            "escalated": self.escalated,
            "callback_requested": self.callback_requested,
            "promise_to_pay": self.promise_to_pay,
            "payment_initiated": self.payment_initiated,
            "hardship": self.hardship_raised,
            "refused_to_pay": self.refusals > 0,
        }
        for name in _DISPOSITION_PRIORITY:
            if flags.get(name):
                return name
        return "no_commitment"

    def call_state_updates(self) -> dict:
        """Call-state fields to write back to the customer context row."""
        updates: dict = {
            "last_disposition": self.disposition(),
            "is_final_transcript": True,
            "interruption_detected": self.interruption_detected,
        }
        if self.verified and not self.wrong_party:
            updates["customer_verified"] = True
        if self.dispute_raised:
            updates["account_disputed"] = True
            updates["payment_status"] = "disputed"
        elif self.payment_verified_status == "completed":
            # The ONLY path that marks an account paid: the backend tool
            # confirmed it. A claim alone (regex or LLM) never writes this.
            updates["payment_status"] = "completed"
        if self.complaint_raised:
            updates["complaint_pending"] = True
        if self.callback_requested:
            updates["callback_requested"] = True
        if self.payment_claimed:
            # The structured payment-verification record (claim, method/date
            # claimed, captured reference, tool outcome) — persisted with the
            # conversation so the follow-up team works from data, not from
            # the transcript.
            updates["payment_verification"] = self.payment_record()
        return updates

    # ── structured state, actions and completion ─────────────────────────

    def payment_record(self) -> dict:
        """The structured payment-claim record for THIS call."""
        return {
            "payment_claimed": self.payment_claimed,
            "payment_method": self.payment_method_claimed,
            "payment_date_claimed": self.payment_date_claimed,
            "transaction_reference": self.transaction_reference,
            "transaction_reference_confirmed": bool(self.transaction_reference),
            "reference_unavailable": self.reference_unavailable,
            "verification_status": self.verification_outcome
            or ("pending" if self.payment_claimed else None),
            "verification_source": self.verification_source,
            "verification_result": self.payment_verified_status,
        }

    def conversation_state(self) -> str:
        """The spec-level conversation state, derived from structured facts."""
        if self._closed:
            return CALL_COMPLETED
        if self.wrong_party:
            return WRONG_PERSON
        if self.payment_claimed:
            if self.verification_outcome == "verified":
                return PAYMENT_VERIFIED
            if self.verification_outcome == "failed":
                return PAYMENT_VERIFICATION_FAILED
            if self.verification_outcome in ("pending", "unverified"):
                return PAYMENT_VERIFICATION_PENDING
            if self.transaction_reference:
                return VERIFYING_PAYMENT
            if self.awaiting_reference:
                return AWAITING_TRANSACTION_REFERENCE
            return PAYMENT_CLAIMED
        if not self.verified:
            if self._identity_unclear_turn or self.identity_unclear_count:
                return IDENTITY_UNCLEAR
            return AWAITING_IDENTITY_CONFIRMATION
        if self.promise_to_pay:
            return PAYMENT_COMMITMENT
        if self._just_verified:
            return IDENTITY_CONFIRMED
        return RECOVERY_DISCUSSION

    def mark_closed(self) -> None:
        """The call actually ended (brain confirmed completion)."""
        self._closed = True

    # Plain class attribute (no annotation — must not become a dataclass field).
    _STATE_ACTIONS = {
        AWAITING_IDENTITY_CONFIRMATION: (
            "ask_identity_confirmation", "confirm_identity",
            "handle_wrong_person",
        ),
        IDENTITY_UNCLEAR: (
            "ask_identity_confirmation", "handle_wrong_person",
            "close_unverified",
        ),
        WRONG_PERSON: ("wrong_person_close", "complete_call"),
        IDENTITY_CONFIRMED: ("state_recovery_purpose", "discuss_recovery"),
        RECOVERY_DISCUSSION: (
            "discuss_recovery", "ask_payment_intent", "record_dispute",
            "record_complaint", "schedule_callback",
        ),
        PAYMENT_CLAIMED: (
            "ask_transaction_reference", "check_existing_payment_records",
        ),
        AWAITING_TRANSACTION_REFERENCE: (
            "ask_transaction_reference", "capture_transaction_reference",
            "clarify_transaction_reference",
        ),
        VERIFYING_PAYMENT: ("verify_payment",),
        PAYMENT_VERIFIED: ("mark_payment_verified", "complete_call"),
        PAYMENT_VERIFICATION_PENDING: (
            "mark_payment_details_recorded", "schedule_follow_up",
            "complete_call",
        ),
        PAYMENT_VERIFICATION_FAILED: (
            "schedule_follow_up", "clarify_transaction_reference",
            "complete_call",
        ),
        PAYMENT_COMMITMENT: (
            "confirm_commitment", "schedule_callback", "complete_call",
        ),
        CALL_COMPLETED: ("complete_call",),
    }
    # Never valid on an outbound recovery call, in any state.
    _ALWAYS_PROHIBITED = (
        "generic_assistance_response",
        "invent_verification_result",
    )

    def allowed_actions(self) -> list[str]:
        actions = list(self._STATE_ACTIONS.get(self.conversation_state(), ()))
        if "handoff_agent" not in actions:
            actions.append("handoff_agent")  # an agent is always reachable
        return actions

    def prohibited_actions(self) -> list[str]:
        """Actions the model must not take NOW (state-specific + global)."""
        prohibited = list(self._ALWAYS_PROHIBITED)
        state = self.conversation_state()
        if not self.verified:
            prohibited += [
                "confirm_identity_on_customers_behalf",
                "disclose_account_details",
            ]
        if state in (PAYMENT_CLAIMED, AWAITING_TRANSACTION_REFERENCE):
            prohibited += [
                "mark_payment_verified", "mark_payment_details_recorded",
                "complete_call", "promise_account_update",
            ]
        elif state == VERIFYING_PAYMENT:
            prohibited += [
                "mark_payment_verified", "complete_call",
                "promise_account_update",
            ]
        elif state in (PAYMENT_VERIFICATION_PENDING,
                       PAYMENT_VERIFICATION_FAILED):
            prohibited += ["mark_payment_verified", "promise_account_update"]
        return prohibited

    def validate_action(self, action: str) -> bool:
        """Executor-side gate: is this action permitted in the current state?

        The reply path never trusts a selected action — completion,
        verification claims and recording claims all require the structured
        state to actually support them.
        """
        if action in self.prohibited_actions():
            return False
        if action == "complete_call":
            return self.evaluate_completion()[0]
        if action == "mark_payment_verified":
            return self.verification_outcome == "verified"
        if action == "mark_payment_details_recorded":
            return bool(self.transaction_reference) or self.reference_unavailable
        if action == "verify_payment":
            return bool(self.transaction_reference)
        return action in self.allowed_actions()

    def evaluate_completion(self) -> tuple[bool, str]:
        """Whether the recovery goal is genuinely achieved.

        Judged from structured state and tool results only — a polite closing
        sentence proves nothing. Mirrors plan_turn's precedence so a close the
        policy planned is always one the evaluator accepts.
        """
        if self.wrong_party:
            return True, "wrong_person_closed"
        if self.escalated:
            return True, "escalated_to_agent"
        if self.dispute_raised:
            return True, "dispute_recorded"
        if self.payment_claimed:
            if self.verification_outcome == "verified":
                return True, "payment_verified"
            if self.verification_outcome == "pending":
                return True, "payment_found_processing"
            if self.verification_outcome in ("failed", "unverified"):
                if self.transaction_reference:
                    return True, "reference_captured_follow_up_recorded"
                return False, "transaction_reference_not_captured"
            if self.reference_unavailable or \
                    self.reference_attempts > _MAX_REFERENCE_ATTEMPTS:
                return True, "customer_could_not_provide_reference"
            return False, "transaction_reference_not_captured"
        if self.callback_requested:
            if self.callback_time_known:
                return True, "callback_scheduled"
            return False, "callback_time_not_captured"
        if not self.verified:
            if self.identity_unclear_count > _MAX_IDENTITY_REASKS:
                return True, "identity_could_not_be_verified"
            return False, "identity_not_confirmed"
        if self.promise_to_pay:
            return True, "payment_commitment_recorded"
        if self.phase == CLOSING:
            return True, "closed"
        return False, "recovery_goal_not_reached"

    def missing_required_fields(self) -> list[str]:
        missing: list[str] = []
        if not self.verified and not self.wrong_party:
            missing.append("identity_confirmation")
        if self.payment_claimed and self.verification_outcome is None:
            if self.transaction_reference is None and not self.reference_unavailable:
                missing.append("transaction_reference")
            elif self.transaction_reference:
                missing.append("payment_verification_result")
        if self.callback_requested and not self.callback_time_known:
            missing.append("callback_time")
        return missing

    # ── prompt construction ──────────────────────────────────────────────

    def placeholder_values(self) -> dict[str, str]:
        """Safe values for {{placeholder}} resolution in authored text.

        Only identity-level values pre-verification; account figures join
        after the customer is verified (greetings must not leak amounts to
        whoever picked up the phone).
        """
        ctx = self.context
        if ctx is None:
            return {}
        values: dict[str, str] = {}
        if ctx.customer_name:
            values["customer_name"] = ctx.customer_name
        if ctx.lender_name:
            values["lender_name"] = ctx.lender_name
        if ctx.dcs_name:
            values["dcs_name"] = ctx.dcs_name
        if self.verified and not self.wrong_party:
            if ctx.overdue_amount is not None:
                values["outstanding_amount"] = _rupees(ctx.overdue_amount)
                values["overdue_amount"] = _rupees(ctx.overdue_amount)
            if ctx.days_overdue is not None:
                values["overdue_days"] = str(ctx.days_overdue)
        return values

    def compact_facts(self) -> list[str]:
        """Identity-gated one-line account facts for the decision call.

        The same disclosure rule as :meth:`turn_instruction`: nothing before
        the caller's identity is confirmed. Short by design — these ground
        the Goal Engine's co-generated replies without duplicating the full
        per-turn prompt block.
        """
        ctx = self.context
        if ctx is None or not self.verified or self.wrong_party:
            return []
        facts: list[str] = []
        if ctx.overdue_amount is not None:
            facts.append(f"overdue amount {_rupees(ctx.overdue_amount)}")
        if ctx.days_overdue is not None:
            facts.append(f"{ctx.days_overdue} days overdue")
        if ctx.due_date:
            facts.append(f"due date {ctx.due_date}")
        if ctx.total_outstanding is not None:
            facts.append(f"total outstanding {_rupees(ctx.total_outstanding)}")
        if ctx.payment_methods:
            facts.append("payment methods " + ", ".join(ctx.payment_methods))
        if ctx.partial_payment_allowed is not None:
            facts.append(
                "partial payment "
                + ("allowed" if ctx.partial_payment_allowed else "not allowed")
            )
        return facts[:8]

    def static_instruction(self) -> str:
        """Once-per-call system-prompt block (customer identity only)."""
        ctx = self.context
        lines = [
            "\n\n# Customer context (server-verified; loaded for THIS call)",
            "A '# Live call state' section is provided fresh on every turn — "
            "it is authoritative and overrides any conflicting script step.",
        ]
        if ctx is None:
            lines.append(
                "No customer record could be loaded for this call. Speak "
                "generically, never guess names or amounts, and offer a "
                "callback from an agent for account-specific questions."
            )
            return "\n".join(lines)
        who = ctx.customer_name or "the account holder"
        lender = ctx.lender_name or "the lender"
        via = (
            f" (calling on behalf of {lender}"
            + (f" through {ctx.dcs_name}" if ctx.dcs_name else "")
            + ")"
        )
        lines.append(f"You are calling {who}{via}.")
        lines.append(
            "Account figures, dates and the masked loan account appear in "
            "the per-turn live-state section ONLY once identity is confirmed."
        )
        return "\n".join(lines)

    def turn_instruction(self) -> str:
        """The per-turn '# Live call state' system-prompt block."""
        ctx = self.context
        parts: list[str] = ["\n\n# Live call state (authoritative — follow exactly)"]

        parts.append(
            "- Call type: outbound_recovery — YOU placed this call about an "
            "overdue payment. Never behave like inbound support: never ask "
            "'How may I help you?' / 'आपकी मदद कैसे कर सकता हूँ?' or any "
            "generic-assistant line, and never drop the recovery objective."
        )
        parts.append(
            "- Recovery goal: confirm you are speaking with the right person, "
            "then resolve the overdue payment — collect it, verify an "
            "already-made payment, or capture a concrete commitment/callback."
        )
        parts.append(f"- Conversation phase: {self.phase}")
        parts.append(f"- Conversation state: {self.conversation_state()}")
        parts.append(
            "- Identity: "
            + ("CONFIRMED — account details may be discussed."
               if self.verified and not self.wrong_party else
               "NOT confirmed — do NOT state amounts, dates, the loan account "
               "or payment history yet. Never claim or assume you are "
               "speaking with the customer: only THEIR clear 'yes' confirms "
               "it. If their answer was unclear, ask again politely.")
        )
        if self.payment_claimed:
            parts.append(
                "- Payment verification: "
                + {
                    "verified": "the system CONFIRMED the payment.",
                    "pending": "the payment shows in the system but is still "
                               "processing — not confirmed yet.",
                    "failed": "the system could NOT find this payment.",
                    "unverified": "NO verification could run on this call — "
                                  "the claim stays unverified.",
                }.get(
                    self.verification_outcome or "",
                    "NOT verified. "
                    + ("The transaction number has been captured but not "
                       "checked yet." if self.transaction_reference else
                       "The transaction number has NOT been provided — "
                       "saying 'yes I have a number' is not the number. "
                       "Never say details were noted, recorded or verified.")
                )
            )
        missing = self.missing_required_fields()
        if missing:
            parts.append(
                "- Required fields still missing: " + ", ".join(missing)
            )
        used_rungs = [
            name for name, used in (
                ("consequence", self.consequence_used),
                ("offer", self.offer_used),
                ("partial payment", self.partial_used),
                ("self-resolution", self.self_resolution_used),
                ("callback/agent options", self.final_options_offered),
            ) if used
        ]
        if used_rungs:
            parts.append(
                "- Recovery pitches already made THIS call (NEVER repeat "
                "any of them): " + ", ".join(used_rungs)
            )
        if self.promise_to_pay:
            parts.append(
                "- Payment commitment so far: amount "
                + (f"{self.proposed_amount} rupees (customer-proposed)"
                   if self.proposed_amount else "not yet agreed")
                + "; date " + ("agreed" if self.promise_date_known else "not set")
                + "; method "
                + (self.payment_method_claimed or "not chosen")
                + ". The customer agreed to pay — no further consequence/"
                "offer/partial pitch is permitted."
            )
        parts.append(
            "- Allowed next actions: " + ", ".join(self.allowed_actions())
        )
        parts.append(
            "- Prohibited now: " + ", ".join(self.prohibited_actions())
        )
        blockers = self.blockers()
        if blockers:
            parts.append("- OPEN ISSUES (unresolved): " + "; ".join(blockers))
            parts.append(
                "- While these are open: no payment requests, no benefit "
                "pitches, no penalty or CIBIL warnings. Resolve or record the "
                "issue and route to verification, callback or a human agent."
            )
        if not self.recording_notice_given and ctx is not None \
                and ctx.recording_notice_required:
            parts.append(
                "- Recording notice pending: state briefly that this call "
                "may be recorded for quality and training."
            )

        # Verified facts (identity-gated).
        if ctx is not None:
            facts: list[str] = []
            if ctx.customer_name:
                facts.append(f"Customer name: {ctx.customer_name}")
            if ctx.lender_name:
                facts.append(f"Lender: {ctx.lender_name}")
            if ctx.dcs_name:
                facts.append(f"Collection agency (DCS): {ctx.dcs_name}")
            if ctx.phone_last4:
                facts.append(
                    "Registered mobile: ending "
                    + " ".join(ctx.phone_last4)
                    + " (NEVER speak more than these last four digits)"
                )
            if ctx.preferred_language:
                facts.append(f"Preferred language: {ctx.preferred_language}")
            if self.verified and not self.wrong_party:
                if ctx.loan_account_masked:
                    facts.append(f"Loan account (masked): {ctx.loan_account_masked}")
                if ctx.overdue_amount is not None:
                    facts.append(f"Overdue amount: {_rupees(ctx.overdue_amount)}")
                if ctx.days_overdue is not None:
                    facts.append(f"Days overdue: {ctx.days_overdue}")
                if ctx.due_date:
                    facts.append(f"Due date: {ctx.due_date}")
                if ctx.total_outstanding is not None:
                    facts.append(f"Total outstanding: {_rupees(ctx.total_outstanding)}")
                if ctx.minimum_payable is not None:
                    facts.append(f"Minimum payable: {_rupees(ctx.minimum_payable)}")
                if ctx.partial_payment_allowed is not None:
                    facts.append(
                        "Partial payment allowed: "
                        + ("yes" if ctx.partial_payment_allowed else "no")
                    )
                if ctx.payment_methods:
                    facts.append("Payment methods: " + ", ".join(ctx.payment_methods))
                if ctx.secure_payment_link_available is not None:
                    facts.append(
                        "Secure payment link available: "
                        + ("yes" if ctx.secure_payment_link_available else "no")
                    )
                if ctx.active_offers:
                    for offer in ctx.active_offers[:3]:
                        label = offer.get("label") if isinstance(offer, dict) else str(offer)
                        terms = offer.get("terms") if isinstance(offer, dict) else None
                        facts.append(
                            "Active offer: " + str(label)
                            + (f" — {terms}" if terms else "")
                        )
                if ctx.offer_terms:
                    facts.append(f"Offer terms: {ctx.offer_terms}")
                if ctx.penal_charges is not None:
                    facts.append(f"Penal charges so far: {_rupees(ctx.penal_charges)}")
                if ctx.credit_reporting_status:
                    facts.append(f"Credit reporting status: {ctx.credit_reporting_status}")
                if ctx.previous_promise_date:
                    facts.append(f"Earlier promise-to-pay date: {ctx.previous_promise_date}")
                if ctx.payment_status:
                    facts.append(f"Payment status on record: {ctx.payment_status}")
                if ctx.callback_number_masked:
                    facts.append(f"Callback number (masked): {ctx.callback_number_masked}")
                if ctx.grievance_contact:
                    facts.append(f"Grievance contact: {ctx.grievance_contact}")
            if facts:
                parts.append(
                    "\n## Verified account facts (the ONLY account facts you may "
                    "state; speak amounts in words, account/reference digits one "
                    "by one)\n" + "\n".join(f"- {fact}" for fact in facts)
                )
            missing = self._missing_facts(ctx)
            if missing:
                parts.append(
                    "\n## Not available on this call (say so honestly and offer "
                    "the app or an agent callback — NEVER guess): "
                    + ", ".join(missing)
                )
        else:
            parts.append(
                "\n## No customer record is available for this call — every "
                "account-specific value (name, amount, dates, account, history) "
                "is UNKNOWN. Say you don't have it on this call and offer an "
                "agent callback. Never guess or invent."
            )

        if self.claims:
            parts.append(
                "\n## Customer statements THIS call (unverified claims — "
                "acknowledge and record them; never confirm them as fact, "
                "never argue, never repeat a request they already declined)\n"
                + "\n".join(f"- \"{claim}\"" for claim in self.claims)
            )

        if self.payment_verified_status is not None:
            parts.append(
                "\n## Backend verification THIS call\n"
                f"- Payment status checked in the system just now: "
                f"{self.payment_verified_status}. This is the ONLY payment "
                "fact you may state as verified."
            )

        parts.append("\n## Your next step\n" + self._next_step())
        tool_rule = (
            "- You cannot run checks yourself; the system performs them and "
            "their results appear above under 'Backend verification'. State "
            "ONLY those verified results — never claim you checked anything "
            "that is not listed there.\n"
            if self.tools_available else
            "- You have NO backend tools on this call: never say you checked, "
            "verified or updated any system, and never ask the customer to "
            "hold while you check something. Verification is done by the team "
            "after the call — say that instead.\n"
        )
        parts.append(
            "\n## Non-negotiable rules for this reply\n"
            "- FIRST respond to what the customer just said; never ignore or "
            "talk past it.\n"
            "- One or two short sentences; at most ONE question.\n"
            + tool_rule +
            "- Never invent payments, transactions, offers, amounts, dates or "
            "customer details. A value not listed above is unknown.\n"
            "- Never repeat a pitch or amount the customer has already "
            "declined or disputed this call.\n"
            "- Never speak a full phone number or full account number."
        )
        return "\n".join(parts)

    def _scripted_opening(self) -> str:
        """The account-explanation opener, or "" to let the LLM answer.

        Returned only when every fact the line states is verified and present.
        Anything conditional — a disputed account, a payment claim, a missed
        promise worth raising, a pending recording notice — is a judgement
        call and goes to the LLM instead, so this can never flatten a nuanced
        situation into a script.
        """
        ctx = self.context
        if ctx is None or not self.verified or self.wrong_party:
            return ""
        if ctx.overdue_amount is None:
            return ""
        if (
            self.dispute_raised
            or self.payment_claimed
            or self.complaint_raised
            or self.callback_requested
            or self.hardship_raised
            or ctx.previous_promise_pending
            or not self.recording_notice_given
        ):
            return ""
        amount = _spoken_rupees(ctx.overdue_amount, self.language)
        if ctx.days_overdue:
            return canned("collections_open_amount_days", self.language).format(
                amount=amount,
                days=_spoken_count(ctx.days_overdue, self.language),
            )
        return canned("collections_open_amount", self.language).format(amount=amount)

    def _missing_facts(self, ctx: CustomerContextSnapshot) -> list[str]:
        if not (self.verified and not self.wrong_party):
            return []
        missing = []
        if ctx.overdue_amount is None:
            missing.append("overdue amount")
        if ctx.due_date is None:
            missing.append("due date")
        if ctx.loan_account_masked is None:
            missing.append("loan account")
        if not ctx.payment_methods:
            missing.append("payment methods")
        return missing

    def _next_step(self) -> str:
        ctx = self.context
        if self.escalated:
            # A transfer is already initiated — nothing may contradict it.
            return (
                "An agent transfer is already in progress. Confirm in ONE "
                "short sentence that they are being connected and should "
                "stay on the line. Never say you cannot transfer them."
            )
        if self.wrong_party:
            return (
                "Apologize sincerely for the inconvenience, do NOT reveal any "
                "account information, say the number will be flagged for "
                "verification so they are not called again, and close the "
                "call politely."
            )
        if self.dispute_raised:
            return (
                "Acknowledge that they dispute this account/amount and say it "
                "has been RECORDED. Do not push payment or consequences. "
                "Offer exactly one next step: a verification callback from "
                "the team or connecting them to an agent"
                + (f" (grievance contact: {ctx.grievance_contact})"
                   if ctx and ctx.grievance_contact else "")
                + ". Then close once they choose."
            )
        if self.payment_claimed:
            if self.verification_outcome == "verified":
                return (
                    "The payment IS confirmed in the system. Thank them, "
                    "apologize briefly for the reminder call, confirm no "
                    "further payment is due right now, and close politely. "
                    "Do not pitch anything."
                )
            if self.verification_outcome == "pending":
                return (
                    "The system shows the payment but it is still processing. "
                    "Say exactly that, tell them it will reflect once "
                    "processed and the team will follow up if needed, and "
                    "close politely. Do NOT call it confirmed."
                )
            if self.verification_outcome == "failed":
                return (
                    "The system could NOT find a payment for the captured "
                    "transaction number. Say that honestly, confirm the "
                    "number is recorded and will be re-checked by the team, "
                    "and close politely. Do NOT accuse them of not paying "
                    "and do NOT demand a new payment."
                )
            if self.verification_outcome == "unverified":
                return (
                    "No verification could run on this call. Say the "
                    "transaction number is noted and verification is PENDING "
                    "with the team. Never say it is verified, recorded as "
                    "paid, or that the account will definitely be updated."
                )
            if self.transaction_reference:
                return (
                    "The transaction number has been captured and is being "
                    "checked. Do not declare any outcome until the check "
                    "result appears under 'Backend verification'."
                )
            if self.awaiting_reference:
                return (
                    "Ask ONE question only: the actual transaction/UTR "
                    "number, digit by digit. It has NOT been provided yet — "
                    "'yes, I have the number' is not the number. Never say "
                    "the details were noted or recorded."
                )
            return (
                "Thank them for the information. Ask ONE question only: the "
                "transaction/reference number of the payment (or when and "
                "how they paid). Do not demand proof, do not push a new "
                "payment, and never claim anything was verified or recorded."
            )
        if self.complaint_raised and self.phase == COMPLAINT_HANDLING:
            return (
                "Apologize briefly and address their actual point. Note the "
                "complaint as recorded"
                + (f"; grievance contact: {ctx.grievance_contact}"
                   if ctx and ctx.grievance_contact else "")
                + ". Offer an agent if they want to take it further."
            )
        if self.callback_requested:
            if self.callback_time_known:
                return (
                    "Confirm the callback time they gave, on their registered "
                    "number (mention only the LAST FOUR digits if needed), "
                    "thank them and close the call politely."
                )
            return (
                "Acknowledge they are busy. Ask ONE question: what time suits "
                "them for a callback. Nothing else."
            )
        if not self.verified and ctx is not None:
            name = ctx.customer_name or "the account holder"
            return (
                f"Politely confirm you are speaking with {name} before any "
                "account discussion. Ask that ONE question only."
            )
        if self.pending_amount_type is not None:
            return self._amount_answer_step()
        if self._medical_turn:
            return (
                "The customer mentioned a medical/family emergency. This is "
                "NOT an ordinary refusal: acknowledge it briefly and "
                "genuinely in one short sentence (vary the wording; never "
                "reuse an earlier acknowledgment), do NOT mention CIBIL, "
                "penalties, offers, partial payment or borrowing this turn, "
                "and do NOT ask for private medical details. Then offer ONE "
                "gentle next step: a callback at a better time or a human "
                "agent. One question only."
            )
        if self._ladder_rung_turn is not None:
            return self._ladder_step(self._ladder_rung_turn)
        if self.promise_to_pay:
            return self._commitment_step()
        if self.hardship_raised:
            return (
                "Respond with genuine empathy, no pressure. Offer a callback "
                "or an agent; if a smaller amount is realistic and partial "
                "payment is allowed, you may mention it ONCE."
            )
        if self.phase in (ACCOUNT_EXPLANATION,):
            return (
                "Explain the overdue status simply using the verified facts "
                "(amount in words, days overdue), then ask ONE question: can "
                "they pay today via the available methods."
            )
        return (
            "Continue the payment discussion from the verified facts: answer "
            "their question, agree the amount (full or partial where allowed) "
            "and the method, and guide them to pay in their own app. One "
            "question at a time; never repeat a declined pitch."
        )

    def _amount_answer_step(self) -> str:
        """Answer-first guidance for an amount question (typed)."""
        mapping = {
            "total": "Total outstanding",
            "overdue": "Overdue amount",
            "minimum": "Minimum payable",
            "penalty": "Penal charges so far",
        }
        refreshed = (
            " The figures above were refreshed from the system THIS turn — "
            "use them, never an older number from the conversation."
            if self.account_refresh_succeeded else ""
        )
        asked = self.pending_amount_type or "ambiguous"
        if asked == "ambiguous":
            return (
                "The customer asked how much to pay (no specific figure "
                "named). Begin the reply DIRECTLY with the amount: state the "
                "verified Total outstanding, then the Minimum payable when "
                "both exist (clearly labelled, amounts in words); with only "
                "one available state just that one. Never guess a missing "
                "figure — say it is not available on this call. Then ask ONE "
                "question: can they make the payment today." + refreshed
            )
        label = mapping[asked]
        return (
            f"The customer asked specifically for the {label}. Begin the "
            f"reply DIRECTLY with that one labelled figure from the verified "
            "facts (amount in words) — do not add other amounts they did not "
            "ask about. If that exact figure is not in the verified facts, "
            "say it is not available on this call and offer the app or an "
            "agent callback — never guess. Then ask ONE question: can they "
            "make the payment today." + refreshed
        )

    def _commitment_step(self) -> str:
        """The next missing payment-commitment element (date → method)."""
        if not self.promise_date_known:
            return (
                "The customer has AGREED to pay — recovery pitches are over: "
                "never mention CIBIL, penalties, discounts, offers or partial "
                "options now. Briefly confirm the payable amount from the "
                "verified facts"
                + (
                    f" (they proposed {self.proposed_amount} rupees — do not "
                    "call it high or low)"
                    if self.proposed_amount else ""
                )
                + " and ask ONE question: the exact date (or today) they "
                "will make the payment."
            )
        if not self.payment_method_claimed:
            return (
                "Amount and date are agreed. Ask ONE question: which of the "
                "available payment methods (from the verified facts) they "
                "will use. Never promise instant account updates."
            )
        return (
            "Amount, date and method are all agreed: repeat the amount and "
            "date back in one short sentence, thank them, and close the call "
            "politely. Do not restart any pitch."
        )

    def _ladder_step(self, rung: str) -> str:
        """Guidance for exactly ONE recovery rung (state-tracked, no repeats)."""
        empathy = (
            "Lead with ONE short, genuine acknowledgment of what they just "
            "said (vary the wording — never reuse an earlier acknowledgment "
            "sentence from this call). "
            if self._hardship_turn else ""
        )
        if rung == "consequence":
            return empathy + (
                "Recovery step (use ONCE): state exactly one consequence "
                "supported by the verified facts — credit-bureau/Sibil "
                "reporting impact on future loan eligibility, or a verified "
                "penalty — factually and preventively, never as a threat, "
                "and never claim their score already fell. Then ask ONE "
                "question: can they make the full payment today. Do not "
                "mention offers, partial payment, savings, callbacks or "
                "agents in this reply."
            )
        if rung == "offer":
            return empathy + (
                "Recovery step (use ONCE): present exactly one active offer "
                "from the verified facts, conditionally ('agar aap eligible "
                "hue', 'up to', 'subject to terms') — never guarantee it, "
                "never invent one. Ask whether they can arrange the payment. "
                "Do not repeat the consequence."
            )
        if rung == "partial":
            return empathy + (
                "Recovery step (use ONCE): offer the verified Minimum "
                "payable as a practical part payment (partial payment is "
                "allowed on this account). Ask ONE question: can they pay "
                "that amount today. Do not repeat the consequence or the "
                "offer, and do not ask the payment method yet."
            )
        if rung == "self_resolution":
            return empathy + (
                "Final recovery step (use ONCE): gently ask what exact "
                "amount they could realistically arrange. You may mention "
                "safe options like savings or family help ONLY as an "
                "optional possibility ('agar aapke liye convenient ho') — "
                "never pressure them to borrow, never cite their job or "
                "studies as proof they can pay. One question."
            )
        if rung == "final_options":
            return empathy + (
                "Every recovery step has been used — do not repeat any of "
                "them. Offer ONE choice: a callback at a better time or a "
                "human agent. If they decline both, thank them and close "
                "politely."
            )
        return (
            "The customer declined every option including a callback/agent. "
            "Acknowledge their decision, thank them for their time, and "
            "close the call politely — no further pitch of any kind."
        )


_ONES_HI = (
    "शून्य एक दो तीन चार पाँच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह "
    "सोलह सत्रह अठारह उन्नीस बीस इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस "
    "सत्ताईस अट्ठाईस उनतीस तीस इकतीस बत्तीस तैंतीस चौंतीस पैंतीस छत्तीस "
    "सैंतीस अड़तीस उनतालीस चालीस इकतालीस बयालीस तैंतालीस चौवालीस "
    "पैंतालीस छियालीस सैंतालीस अड़तालीस उनचास पचास इक्यावन बावन तिरपन "
    "चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ इकसठ बासठ तिरसठ चौंसठ "
    "पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर सत्तर इकहत्तर बहत्तर तिहत्तर चौहत्तर "
    "पचहत्तर छिहत्तर सतहत्तर अठहत्तर उन्यासी अस्सी इक्यासी बयासी तिरासी "
    "चौरासी पचासी छियासी सतासी अठासी नवासी नब्बे इक्यानबे बानबे तिरानबे "
    "चौरानबे पचानबे छियानबे सत्तानबे अट्ठानबे निन्यानबे"
).split()


def _hindi_int_words(n: int) -> str:
    """Indian-system Hindi words for a non-negative integer (crore/lakh)."""
    if n < 100:
        return _ONES_HI[n]
    parts: list[str] = []
    for unit, label in ((10**7, "करोड़"), (10**5, "लाख"), (1000, "हज़ार")):
        if n >= unit:
            parts.append(f"{_hindi_int_words(n // unit)} {label}")
            n %= unit
    if n >= 100:
        parts.append(f"{_ONES_HI[n // 100]} सौ")
        n %= 100
    if n:
        parts.append(_ONES_HI[n])
    return " ".join(parts)


def _spoken_rupees(amount: float, language: str | None) -> str:
    """Amount as it should be SPOKEN — words, never digits.

    Distinct from :func:`_rupees`, which is written for the LLM's prompt and
    deliberately carries both digits and a pronunciation hint. This one goes
    straight to the TTS, where a digit string is exactly what mispronounces.
    """
    whole = int(amount)
    if (language or "").split("-")[0].lower() == "hi" and amount == whole and 0 <= whole < 10**9:
        return f"{_hindi_int_words(whole)} रुपये"
    return f"{whole:,} rupees" if amount == whole else f"{amount:,.2f} rupees"


def _spoken_count(value: int, language: str | None) -> str:
    """A small count as words, for the same reason as :func:`_spoken_rupees`."""
    if (language or "").split("-")[0].lower() == "hi" and 0 <= value < 10**9:
        return _hindi_int_words(value)
    return str(value)


def _rupees(amount: float) -> str:
    """Amount for the prompt: digits PLUS pre-verbalized Hindi words.

    Small models reliably garble digit→word conversion mid-stream ("पचास सौ
    चौसठ रुपये"), so the exact spoken form is supplied rather than trusted
    to the model. Paise are rare in collections and are kept as digits.
    """
    whole = int(amount)
    digits = f"₹{whole:,}" if amount == whole else f"₹{amount:,.2f}"
    if amount == whole and 0 <= whole < 10**9:
        return f"{digits} (speak as: {_hindi_int_words(whole)} रुपये)"
    return digits
