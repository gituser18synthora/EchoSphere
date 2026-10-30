"""Every Haier-specific value in ONE place (tenant tn_fb39b67f5361, bot
bot_aba8f9101217). Nothing in shared/ or voice_runtime/ knows any of this.

Sources of truth:
  tenant/Haier/Rejection_Script_24_March_26.xlsx  (Sheet1 linear script,
      Sheet2 decision table with Action 1 / Action 2 and the 5-day rule)
  tenant/Haier/call_transcripts.md                 (8 real agent calls)
  tenant/Haier/HAIER_BOT_UPDATE_PROMPT.md          (requirements)

Agent gender: the authored Hinglish is generated in the gender of the bot's
selected voice (vp-sv-shubh = male). Switching to a female voice means
setting AGENT_GENDER = "F" and re-running 02_prompts + 03_workflow. The
platform's speaker-grammar adapter (raha/rahi, sakta/sakti, -unga/-ungi)
is a second safety net for fixed text.
"""

TENANT = "tn_fb39b67f5361"
BOT = "bot_aba8f9101217"
WORKFLOW_ID = "wf_d5df4464f348"           # existing row; PUT overwrites in place
WORKFLOW_NAME = "Haier rejected work order follow-up"
VOICE = "vp-sv-shubh"
AGENT_GENDER = "M"                         # M | F — must match VOICE's gender

BRAND = "Haier"
HELPLINE_PLACEHOLDER = "1800 000 0000"     # TEST ONLY — the dialer sends the real number

# Logins are NOT stored here. Stages read them from the environment:
#   HAIER_ADMIN_EMAIL / HAIER_ADMIN_PASSWORD  — an existing Haier tenant-admin login
#                                               (e.g. admin@haier.com); needed ONLY for
#                                               PUT /tenant/turn-detection (tenant_admin role only)
#   ECHOSPHERE_SUPER_ADMIN_EMAIL / _PASSWORD  — platform super admin for everything else
#                                               (defaults to the local dev admin)

GUARDRAIL_PROFILE_CODE = "haier_standard_payment_restricted"
GUARDRAIL_PROFILE_NAME = "Haier Standard (payment restricted)"
GUARDRAIL_RULES = ("profanity_deescalation", "payment_collection_restriction")

# Dialer / webhook variables (see tenant/Haier/WEBHOOK_CONTRACT.md).
CONTEXT_FIELDS = [
    {"key": "customer_name", "type": "string", "label": "Customer name"},
    {"key": "customer_gender", "type": "string", "label": "Customer gender (M/F, optional)"},
    {"key": "wo_number", "type": "string", "label": "Work Order number"},
    {"key": "product_name", "type": "string", "label": "Product"},
    {"key": "l3_tagging", "type": "string", "label": "L3 tagging (Installation/Service/Complaint/Demo)"},
    {"key": "reject_reason", "type": "string", "label": "Reject reason (one of 8 labels)"},
    {"key": "request_date", "type": "string", "label": "WO registration date (YYYY-MM-DD)"},
    {"key": "helpline_number", "type": "string", "label": "Haier toll-free helpline"},
]
TEST_PAYLOAD = {
    "customer_name": "Vinay",
    "wo_number": "WO2601184168338",
    "product_name": "AC",
    "l3_tagging": "Service",
    "reject_reason": "Consumer Will Call Back",
    "request_date": "2026-09-29",
    "helpline_number": HELPLINE_PLACEHOLDER,
}
MISSING_VALUE_POLICY = (
    "Never guess the work order number, product, reject reason, dates, charges "
    "or the helpline number. If a value is missing, speak generically (aapki "
    "request, helpline number) and never read a placeholder aloud."
)

REJECT_REASONS = [
    "Bill not available",
    "Consumer Will Call Back",
    "No Response",
    "Product Not Delivered",
    "E-Com DOA Case",
    "Refuse to Pay",
    "Unit OK/No Defect Found",
    "Not Haier Product",
]


def g(male: str, female: str) -> str:
    """Pick the speaker-gender form for authored first-person Hinglish."""
    return female if AGENT_GENDER.upper().startswith("F") else male


ADDR = "{{customer_name}} ji"              # default address; Sir/Ma'am only via LLM rule
AGENT = "{{voice_speaker_name}}"

# ── Script lines (Roman Hinglish, official script wins) ─────────────────────
GREETING = (
    f"Namaskar, {BRAND} ke Customer Care se {{voice_speaker_name}} baat kar "
    f"{g('raha', 'rahi')} hoon. Kya meri baat {{customer_name}} ji se ho rahi hai?"
)
PURPOSE = (
    "Ye call aapke {{product_name}} ki {{l3_tagging}} request ke regarding ki gayi "
    "hai, jo hamari Service Center ki taraf se reject hokar aayi hai, "
    "{{reject_reason}} ke reason mein."
)
TECH_Q = (
    f"Kya main jaan {g('sakta', 'sakti')} hoon, aapke paas {BRAND} ke technician "
    "ki taraf se koi call aaya tha ya koi visit hui thi?"
)
TECH_Q_SHORT = (
    f"Kya aapke paas {BRAND} ke technician ki taraf se koi call aaya tha ya koi "
    "visit hui thi?"
)
TECH_YES_ACK = "Confirm karne ke liye dhanyavad."
APOLOGY_NO_CONTACT = (
    f"Maafi {g('chahunga', 'chahungi')} ki aapko {BRAND} ke technician ki taraf se "
    "koi bhi call ya visit nahi hui."
)
EMPATHY = f"Maafi {g('chahunga', 'chahungi')} asuvidha ke liye."
REASON_INTRO = (
    f"Jaise ki main check kar pa {g('raha', 'rahi')} hoon, yahan pe aapki call "
    "{{reject_reason}} ke reason ki wajah se reject hui hai."
)
REASON_APOLOGY = (
    f"Maafi {g('chahunga', 'chahungi')} ki aapki request is reason ki wajah se "
    "reject hui."
)
Q_BILL = (
    f"Kya main jaan {g('sakta', 'sakti')} hoon, kya aapke paas bill available tha "
    "technician ke visit ke time pe?"
)
Q_CHARGES = "Kya aap charges dene ke liye ready hain?"
Q_CWCB = (
    f"Kya main jaan {g('sakta', 'sakti')} hoon, jab hamare technician ne call kiya "
    "tha tab aapne service ke liye mana kiya tha?"
)
Q_NORESP = (
    f"Kya main jaan {g('sakta', 'sakti')} hoon, kya aapko call receive hui thi "
    "service center ya technician ki taraf se?"
)
Q_NORESP_TALK = "Aapki technician se kya baat hui thi?"
Q_PND = (
    f"Kya main jaan {g('sakta', 'sakti')} hoon, kya ab aapka product deliver ho "
    "gaya hai?"
)
Q_PND_DELIVERED_WHEN = "Aapka product kab deliver hua tha?"
Q_PND_EXPECTED = "Aapka product kab tak deliver hoga?"
PND_CANCEL = (
    "Theek hai. Jab aapka product deliver ho jaye, tab aap Haier ke helpline "
    "number {{helpline_number}} par call karke ek nayi request raise karwa "
    f"dijiyega. Abhi main ye request cancel kar {g('raha', 'rahi')} hoon."
)
Q_DOA = (
    f"Kya main jaan {g('sakta', 'sakti')} hoon, kya aapko E-com site se product "
    "damaged deliver hua hai?"
)
DOA_CANCEL = (
    "Confirmation ke liye dhanyavad. Jaise ki aapne confirm kiya ki product "
    f"damage receive hua hai, main ye call cancel kar {g('raha', 'rahi')} hoon. "
    "Aap E-com site se product replace karwa ke hamare helpline number "
    "{{helpline_number}} pe call karke ek new request raise karwa dijiyega."
)
Q_RTP = (
    f"Kya main jaan {g('sakta', 'sakti')} hoon, kya aapne technician ke visit ke "
    "time pe payment karne se mana kar diya tha?"
)
RTP_WARN = (
    f"Main aapko batana {g('chahunga', 'chahungi')}, agar aap payment nahi karte "
    "hain to aapki request cancel kar di jayegi."
)
Q_UNITOK = (
    f"Kya main jaan {g('sakta', 'sakti')} hoon, kya aapke product mein abhi koi "
    f"problem hai, ya problem {BRAND} ke technician ke dwara resolve ho chuki hai?"
)
Q_DONE_WHO = f"Kya ye kaam {BRAND} ke technician ne kiya tha?"
DONE_TECH_THANKS = (
    "Feedback ke liye dhanyavad. Main is request ko completion ke liye aage "
    f"forward kar {g('raha', 'rahi')} hoon."
)
UNITOK_CANCEL = (
    "Jaisa ki aapne confirm kiya ki product OK hai, is request ko cancel kiya ja "
    "raha hai."
)
Q_NHP = (
    f"Kya main jaan {g('sakta', 'sakti')} hoon, kya aapke paas koi {BRAND} ka "
    "product hai jiski aapne request raise karwai thi?"
)
NHP_CANCEL = (
    "Confirmation ke liye dhanyavad. Aap jis company ka product use kar rahe hain, "
    "us company ke helpline number pe sampark kar sakte hain. Aapki ye request "
    "cancel ki ja rahi hai."
)
Q_NHP_VERIFY = f"Kya aapke paas {BRAND} ka koi product hai?"
Q_NHP_VERIFY_2 = (
    f"Koi bhi {BRAND} ka product, jaise fridge, AC ya washing machine, jiske liye "
    "request ki gayi ho?"
)
NHP_WRONG_CANCEL = (
    f"{EMPATHY} Jaisa ki aap bata rahe hain, aapke paas koi {BRAND} ka product "
    f"nahi hai. Main yahan se ye request cancel kar {g('raha', 'rahi')} hoon."
)
Q_SERVICE = f"Kya aapko {BRAND} ki taraf se service ki requirement hai?"
Q_SERVICE_SIMPLE = f"Kya aapko abhi {BRAND} ki taraf se service chahiye, haan ya nahi?"
NO_REQ_CANCEL = (
    "Jaise ki aapko service ki requirement nahi hai, main aapki request ko cancel "
    f"kar {g('raha', 'rahi')} hoon."
)
UNCLEAR_CANCEL = (
    f"Theek hai. Abhi main yahan se request ko close kar {g('raha', 'rahi')} hoon."
)
Q_DATE = "Kis date ko aap technician ki visit chahte hain?"
Q_DATE_CLARIFY = "Kya aap agle paanch din ke andar technician ki visit karwana chahenge?"
Q_FUTURE_CONFIRM = (
    "Matlab aapko service abhi nahi, baad mein chahiye. Kya main sahi samajh "
    f"{g('raha', 'rahi')} hoon?"
)
FUTURE_DENY = (
    f"{ADDR}, abhi main yahan se request ko close kar {g('raha', 'rahi')} hoon. "
    "Jab aapko requirement ho, tab aap helpline number {{helpline_number}} par "
    "call karke ek nayi request raise karwa sakte hain."
)
ACTION1_REDISPATCH = (
    f"Main aapki request ko re-dispatch kar {g('raha', 'rahi')} hoon taki aapke "
    "concern ke liye technician visit kar sake. Technician call karke aapke place "
    "par jald se jald visit karenge."
)
ACTION2_HELPLINE = (
    "Jab bhi aapko requirement ho, aap Haier ke helpline number {{helpline_number}} "
    "par call karke ek nayi request raise karwa sakte hain."
)
GIFT_Q = "Kya aap ye product kisi aur ko gift kar rahe hain?"
GIFT_DATE_Q = "Theek hai. Installation ki requirement kab hogi?"
GIFT_ADVICE = (
    "Jab bhi installation ki requirement ho, aap jinhe gift kar rahe hain unhe "
    "inform kar dijiyega ki woh apne number se Haier ke helpline number "
    "{{helpline_number}} par call karke request raise karwa lein. Abhi main ye "
    f"request cancel kar {g('raha', 'rahi')} hoon."
)
RESALE_Q = "Kya aapne ye product aage sale karne ke liye liya hai?"
RESALE_CANCEL = (
    "Theek hai. Jaisa ki aap bata rahe hain, aapne ye product sale karne ke liye "
    f"liya hai, main yahan se request cancel kar {g('raha', 'rahi')} hoon. Aapko is "
    "request ke liye dobara call nahi aayega."
)
WRONG_PERSON_Q = f"Maaf kijiye. Kya {ADDR} abhi available hain?"
WRONG_PERSON_WAIT = "Theek hai, main line par hoon."
CALLBACK_CLOSE = "Theek hai, hum baad mein call karenge."
DNC_ASSURE = f"{EMPATHY} Abhi aapke paas dobara call nahi aayega."
ESCALATE = (
    f"{EMPATHY} Main aapki request hamari senior team ko note kar "
    f"{g('raha', 'rahi')} hoon, woh aapko jald hi call karenge."
)
CLOSING = (
    f"Thank you {ADDR}, {BRAND} mein apna keemti samay dene ke liye dhanyavad. "
    "Aapka din shubh rahe."
)
CLOSING_NEUTRAL = (
    f"{BRAND} mein apna keemti samay dene ke liye dhanyavad. Aapka din shubh rahe."
)

# ── 5-day rule lexicon (tenant heuristic; exact day-count = platform request) ─
# synonymPatterns are tried in authored order: "later" first so a sentence
# that contains both ("kal nahi, agle hafte") lands on the conservative
# branch, which the flow then CONFIRMS before cancelling.
_W = r"(?<![a-z])"
_E = r"(?![a-z])"
DATE_BUCKET_PATTERNS = {
    "later": [
        _W + r"(?:\d+|ek|do|teen|chaar|char|paanch|panch|chhe|che|saat|aath|nau|das|dus|pandrah|bees|tees)\s*(?:-\s*)?(?:mahine|mahina|month|months|hafte|hafta|haftey|week|weeks|saal)" + _E,
        _W + r"(?:[6-9]|[1-9]\d+|chhe|che|saat|aath|nau|das|dus|pandrah|bees|tees)\s*(?:-\s*)?(?:din|dino|days?)" + _E,
        _W + r"(?:agle|agla|next)\s*(?:hafte|hafta|mahine|mahina|week|month)" + _E,
        _W + r"out\s*of\s*(?:station|state|town|city)" + _E,
        _W + r"(?:bahar|baahar)\s*(?:hoon|hu|hain|hai|gaya|gayi|gaye)" + _E,
        _W + r"shift(?:ing)?" + _E,
        _W + r"(?:wapas|wapis)\s*aa" ,
        _W + r"baad\s*me(?:in)?(?!\s*call)" + _E,
        _W + r"abhi\s*nahi\s*(?:hogi|hoga|hogaa|karwana|karvana|karana|karna|chahiye|chaahiye|karenge|karwayenge|karvayenge)" + _E,
        _W + r"(?:abhi\s*)?rakh(?:a|i)\s*(?:hua|hui)" + _E,
        _W + r"(?:shaadi|shadi|wedding|function)\s*ke\s*baad" + _E,
        _W + r"later" + _E,
    ],
    "within": [
        _W + r"abhi\s*(?:hi|available|karwa|karva|kar\s*do|chahiye|bhej|aa\s*jao|aa\s*jaye|karo|kar\s*sakte)" + _E,
        _W + r"(?:aaj|aj|today|kal|kalko|tomorrow|parso|parson)" + _E,
        _W + r"(?:ek|1|do|2|teen|3|chaar|char|4|paanch|panch|5)\s*(?:-\s*)?(?:ya\s*)?(?:do|teen|chaar|char|paanch|panch|\d)?\s*(?:din|dino|days?)" + _E,
        _W + r"(?:is|this)\s*(?:hafte|hafta|week)" + _E,
        _W + r"(?:jaldi|jald\s*se\s*jald|turant|urgent)" + _E,
        _W + r"available\s*(?:hoon|hu|hain|hai)" + _E,
    ],
}
# The raw date phrase kept for the audit trail (required_date_text).
# LATER alternatives are harvested on every hub; WITHIN alternatives only
# where the question is about the visit date itself (a technician's quoted
# "aaj hi karao" at the technician check must never pre-fill the visit date).
_NUM = r"(?:\d+|ek|do|teen|chaar|char|paanch|panch|chhe|che|saat|aath|nau|das|dus|pandrah|bees|tees)"
DATE_PHRASE_LATER_ALTS = [
    r"(?<![a-z])" + _NUM + r"\s*(?:-\s*)?(?:mahine|mahina|month|months|hafte|hafta|week|weeks|saal)(?:\s*(?:ke\s*)?(?:baad|bad|me|mein|tak))?",
    r"(?<![a-z])(?:[6-9]|[1-9]\d+|chhe|che|saat|aath|nau|das|dus|pandrah|bees|tees)\s*(?:-\s*)?(?:din|dino|days?)(?:\s*(?:ke\s*)?(?:baad|bad|me|mein|tak))?",
    r"(?<![a-z])\d{1,2}\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:\s*\d{4})?",
    r"(?<![a-z])(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*\d{1,2}(?![0-9])",
    r"(?<![a-z])\d{1,2}\s*(?:tareekh|tarikh)",
    r"(?<![0-9])\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?(?![0-9])",
    r"(?<![a-z])(?:agle|agla|next)\s*(?:hafte|hafta|mahine|mahina|week|month)",
    r"(?<![a-z])out\s*of\s*(?:station|state|town|city)(?:\s*(?:hoon|hu|hain|hai|tha|thi))?",
    r"(?<![a-z])shift(?:ing)?(?:\s*(?:ke\s*baad|hone\s*ke\s*baad))?",
    r"(?<![a-z])(?:shaadi|shadi|wedding)\s*ke\s*baad",
    r"(?<![a-z])abhi\s*nahi\s*(?:hogi|hoga|karwana|karvana|karana|karna|chahiye|karenge|karwayenge)",
    r"(?<![a-z])baad\s*me(?:in)?(?!\s*call)",
]
DATE_PHRASE_WITHIN_ALTS = [
    r"(?<![a-z])(?:ek|1|do|2|teen|3|chaar|char|4|paanch|panch|5)\s*(?:-\s*)?(?:ya\s*)?(?:do|teen|chaar|char|paanch|panch|\d)?\s*(?:din|dino|days?)(?:\s*(?:ke\s*)?(?:baad|bad|me|mein|tak))?",
    r"(?<![a-z])(?:is|this)\s*(?:hafte|hafta|week)",
    r"(?<![a-z])(?:aaj|today|kal|tomorrow|parso|parson)(?:\s*(?:hi|ko|subah|shaam|tak))?",
    r"(?<![a-z])abhi\s*(?:hi|available|karwa|karva|kar\s*do|chahiye)",
    r"(?<![a-z])available\s*(?:hoon|hu|hain|hai)",
    r"(?<![a-z])(?:jaldi|jald\s*se\s*jald|turant)",
]
# regexPatterns are tried IN ORDER (first pattern that matches anywhere wins),
# so specific durations/dates come first and vague phrases ("baad mein") last.
_VAGUE = {r"(?<![a-z])abhi\s*nahi\s*(?:hogi|hoga|karwana|karvana|karana|karna|chahiye|karenge|karwayenge)",
          r"(?<![a-z])baad\s*me(?:in)?(?!\s*call)"}
_LATER_SPECIFIC = [a for a in DATE_PHRASE_LATER_ALTS if a not in _VAGUE]
_LATER_VAGUE = [a for a in DATE_PHRASE_LATER_ALTS if a in _VAGUE]
DATE_PATTERNS_LATER = ["(?i)(" + a + ")" for a in _LATER_SPECIFIC + _LATER_VAGUE]
DATE_PATTERNS_FULL = ["(?i)(" + a + ")" for a in _LATER_SPECIFIC + DATE_PHRASE_WITHIN_ALTS + _LATER_VAGUE]
DATE_PHRASE_REGEX_LATER = "(?i)(" + "|".join(DATE_PHRASE_LATER_ALTS) + ")"       # single-regex form
DATE_PHRASE_REGEX = "(?i)(" + "|".join(DATE_PHRASE_LATER_ALTS + DATE_PHRASE_WITHIN_ALTS) + ")"
DATE_BUCKET_PATTERNS_LATER_ONLY = {"later": DATE_BUCKET_PATTERNS["later"]}
# technician-contact fact harvested from statements that answer another hub
TECH_CONTACT_PATTERNS = {
    "no": [r"(?<![a-z])(?:koi|kisi ne|kuch)?\s*(?:call|phone|visit|contact)\s*(?:bhi\s*)?nahi\s*(?:aaya|aayi|hua|hui|kiya|ki)",
           r"(?<![a-z])(?:koi|kuch)\s*nahi\s*aaya", r"(?<![a-z])nahi\s*aaya\s*(?:koi|abhi)?", r"(?<![a-z])nobody\s*called"],
    "yes": [r"(?<![a-z])(?:call|phone|visit)\s*(?:aaya|aayi|aya|hui|hua|kiya|kiya gaya|mila|mili)",
            r"(?<![a-z])(?:aaya|aayi|aya)\s*(?:tha|thi)(?:\s*call)?", r"(?<![a-z])(?:kiya|ki)\s*gaya\s*tha",
            r"(?<![a-z])technician\s*(?:aaya|aaye|ne\s*call)", r"(?<![a-z])baat\s*hui\s*thi"],
}

# ── Structured disposition (goal_policy.summaryFields) ──────────────────────
CANCEL_SUB_REASONS = [
    "future_date", "gift", "resale", "not_haier", "product_not_delivered",
    "ecom_doa", "unit_ok", "refuse_to_pay", "no_requirement", "wrong_number",
    "unclear",
]
SUMMARY_FIELDS = [
    {"name": "wo_number", "label": "WO number", "type": "text", "source": "wo_number",
     "description": "Work order number this call was about.", "allowLlm": False},
    {"name": "customer_reached", "label": "Customer reached", "type": "yes_no",
     "source": "customer_reached", "allowLlm": True,
     "description": "Yes when the named customer confirmed identity and the flow ran."},
    {"name": "technician_contacted", "label": "Technician contacted", "type": "choice",
     "source": "technician_contacted", "options": ["yes", "no", "unknown"], "allowLlm": True,
     "description": "Did a Haier technician call or visit before the rejection?"},
    {"name": "reject_reason_confirmed", "label": "Reject reason", "type": "text",
     "source": "reject_reason", "allowLlm": False,
     "description": "Reject reason label supplied by the dialer."},
    {"name": "service_required", "label": "Service required", "type": "choice",
     "source": "service_required", "options": ["yes", "no", "later"], "allowLlm": True,
     "description": "Customer's current service requirement."},
    {"name": "required_date_text", "label": "Required date (as said)", "type": "text",
     "source": "required_date_text", "allowLlm": False,
     "description": "Customer's own words for when they want the visit — audit trail for the 5-day rule."},
    {"name": "within_5_days", "label": "Within 5 days", "type": "choice",
     "source": "within_5_days", "options": ["yes", "no", "unknown"], "allowLlm": False,
     "description": "Heuristic 5-day rule result (lexicon-based)."},
    {"name": "action", "label": "Action", "type": "choice", "source": "action",
     "options": ["RE_DISPATCH", "CANCEL", "NO_RESPONSE", "ESCALATE", "CALLBACK"], "allowLlm": True,
     "description": "Outcome. NO_RESPONSE only when nobody answered (filled post-call)."},
    {"name": "cancel_sub_reason", "label": "Cancel sub-reason", "type": "choice",
     "source": "cancel_sub_reason", "options": CANCEL_SUB_REASONS, "allowLlm": False,
     "requires": {"action": ["CANCEL"]},
     "description": "Why the WO was cancelled."},
    {"name": "dnc_requested", "label": "Do-not-call requested", "type": "yes_no",
     "source": "dnc_requested", "allowLlm": True,
     "description": "Customer asked not to be called again."},
    {"name": "hook_status", "label": "CRM hook status", "type": "choice",
     "source": "hook_status", "options": ["ok", "failed"], "allowLlm": False,
     "description": "Whether the re-dispatch/cancel API accepted the call."},
    {"name": "customer_sentiment", "label": "Customer sentiment", "type": "choice",
     "options": ["neutral", "irritated", "angry", "positive"], "allowLlm": True,
     "description": "Overall tone of the customer."},
    {"name": "notes", "label": "Notes", "type": "text", "allowLlm": True,
     "description": "Short free-text summary, Hinglish OK."},
]
GOAL_POLICY = {"endCall": {"enabled": True}, "summaryFields": SUMMARY_FIELDS}

# ── CRM hooks (placeholder endpoints; reserved .example TLD never resolves) ──
HOOK_HOST = "https://crm-integration.haier.example/api/v1"
CONN_REDISPATCH = "Haier Re-dispatch Work Order"
CONN_CANCEL = "Haier Cancel Work Order"
CONN_DNC = "Haier Set Do Not Call"
API_CONTEXT_ARGS = ["wo_number", "customer_name", "product_name", "l3_tagging",
                    "reject_reason", "request_date"]
CONNECTIONS = [
    {
        "name": CONN_REDISPATCH,
        "description": ("Action 1 — re-dispatch the rejected work order so a technician "
                        "visits. Placeholder endpoint until Haier shares the real CRM API; "
                        "the .example host guarantees the failure edge on live, and the "
                        "sample response in responseSchema.example drives mocked tests."),
        "method": "POST", "url": f"{HOOK_HOST}/work-orders/redispatch",
        "isStateChanging": True, "requireConfirmation": False,
        "timeoutMs": 6000, "retries": 1,
        "bodyTemplate": {"source": "echosphere_rejection_bot", "action": "RE_DISPATCH"},
        "requestSchema": {"type": "object", "properties": {
            "wo_number": {"type": "string"}, "reject_reason": {"type": "string"},
            "technician_contacted": {"type": "string"}, "service_required": {"type": "string"},
            "required_date_text": {"type": "string"}, "within_5_days": {"type": "string"}},
            "required": []},
        "responseSchema": {"type": "object", "properties": {
            "status": {"type": "string"}, "wo_number": {"type": "string"},
            "dispatch_reference": {"type": "string"}},
            "example": {"status": "REDISPATCHED", "wo_number": "WO2601204174136",
                        "dispatch_reference": "RD-2026-000123"}},
        "responseMapping": [{"source": "status", "target": "hook_result"},
                            {"source": "dispatch_reference", "target": "dispatch_reference"}],
    },
    {
        "name": CONN_CANCEL,
        "description": ("Action 2 — cancel/close the work order as per customer VOC. "
                        "Placeholder endpoint (see re-dispatch connection notes)."),
        "method": "POST", "url": f"{HOOK_HOST}/work-orders/cancel",
        "isStateChanging": True, "requireConfirmation": False,
        "timeoutMs": 6000, "retries": 1,
        "bodyTemplate": {"source": "echosphere_rejection_bot", "action": "CANCEL"},
        "requestSchema": {"type": "object", "properties": {
            "wo_number": {"type": "string"}, "cancel_sub_reason": {"type": "string"},
            "required_date_text": {"type": "string"}, "service_required": {"type": "string"}},
            "required": []},
        "responseSchema": {"type": "object", "properties": {
            "status": {"type": "string"}, "wo_number": {"type": "string"},
            "cancel_reference": {"type": "string"}},
            "example": {"status": "CANCELLED", "wo_number": "WO2601204174136",
                        "cancel_reference": "CN-2026-000456"}},
        "responseMapping": [{"source": "status", "target": "hook_result"},
                            {"source": "cancel_reference", "target": "cancel_reference"}],
    },
    {
        "name": CONN_DNC,
        "description": ("Suppress further outbound calls for this WO/customer when they "
                        "ask not to be called again. Placeholder endpoint."),
        "method": "POST", "url": f"{HOOK_HOST}/customers/do-not-call",
        "isStateChanging": True, "requireConfirmation": False,
        "timeoutMs": 6000, "retries": 1,
        "bodyTemplate": {"source": "echosphere_rejection_bot", "dnc_requested": True},
        "requestSchema": {"type": "object", "properties": {
            "wo_number": {"type": "string"}}, "required": []},
        "responseSchema": {"type": "object", "properties": {"status": {"type": "string"}},
                           "example": {"status": "DNC_SET"}},
        "responseMapping": [{"source": "status", "target": "dnc_hook_result"}],
    },
]
MOCKS = {c["name"]: c["responseSchema"]["example"] for c in CONNECTIONS}

# ── Tenant runtime behaviour ────────────────────────────────────────────────
TIMEZONE = "Asia/Kolkata"
TURN_DETECTION = {
    "mode": "custom",
    "overrides": {
        # existing browser overrides kept as-is
        "browser": {"turn_detection": {"silence_retry_seconds": 9, "silence_prompt_seconds": 9}},
        # telephony: "kya aap mujhe sun paa rahe hain?" at most 2 times, then close
        "telephony": {"turn_detection": {"silence_prompt_seconds": 8,
                                          "silence_retry_seconds": 8,
                                          "silence_max_prompts": 2}},
    },
}
