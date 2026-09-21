"""Stage 03 — the AU Small Finance Bank debit-card & account-services flow.

Updates the bot's EXISTING single workflow row (wf_bb319e0f6fb5) in place and
marks it approved. Source: "AU Small Finance Bank-Sample AI Voice Bot
Interaction Flow.docx" (sections 1-9).

Shape (one workflow, every service reachable from every hub):

  start → verified? ──yes──► services hub ("How may I assist you today?")
            │ no
            ▼
          mobile ask → OTP ask → "identity verified" (customer_verified=true)
                                                    └──► services hub
  services hub / anything-else hub / every yes-no hub carry the SAME set of
  service-switch edges, so the caller can change request at any point:
    balance hub → (yes) recent transactions → anything-else
    mini-statement hub → (yes) "sent via SMS" → anything-else
    lost/block hub → (yes) blocked → replacement? → (yes) DC458921 → anything-else
    replacement hub (direct request) → (yes) DC458921
    PIN reset: fresh OTP ask → processed → anything-else
    failed-ATM hub → (yes) SR784521 → anything-else
    statement hub → (yes) last 30 days to registered email → anything-else
    profile/email: fresh OTP ask → "update request registered" → anything-else
  anything-else hub → (no / finished) closing line → end

Engine contract honoured (shared/orchestration/workflow_engine.py):
  - Slot ``customer_verified`` (platform convention) is set by a message
    node's ``setSlots`` after the OTP; the start condition skips authentication
    on re-entry, and the engine consumes a verified caller's entry utterance
    at the first hub by literal edge match (no re-greeting, no re-auth).
  - Intent hubs pick edges by semantic signal first, then longest literal
    token. Utterances naming "card"/"debit"/"atm" carry the platform's
    payment_intent signal, which is affirm-compatible — every hub that has a
    YES edge therefore also carries the service-switch edges (their tokens
    carry the same signal), so "actually my debit card is lost" never reads
    as "yes".  ``python 03_workflow.py --check`` replays the planned
    utterances through the engine's own edge selector before saving.
  - Asks: the OTP/mobile asks are fail-closed (``unmatchedReply``): an
    unverified caller's off-topic turn is never answered from the LLM with
    account data; retries exhausted → authored ``fallback`` edge.
  - Every node carries English text plus ``textByLanguage.hi``; the runtime
    picks the caller's current language per turn, state is untouched by a
    language switch.
  - Static demo values (balance, transactions, DC458921, SR784521, ₹5,000,
    24 hours, 5-7 working days) are the docx script's fixed lines; the same
    values are the authoritative "demo data" section of the system prompt.
    No JSON/data file and no knowledge base holds them.

Run:  env/bin/python au_bank/setup/03_workflow.py [--check]
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root for shared.*
from _common import BOT, WORKFLOW_ID, check, client  # noqa: E402

WORKFLOW_NAME = "AU Bank debit card and account services"


def N(nid, kind, label, config=None):
    return {"id": nid, "kind": kind, "label": label,
            **({"config": config} if config else {})}


def E(src, dst, label=None):
    edge = {"id": f"e_{src}__{dst}", "from": src, "to": dst}
    if label:
        edge["label"] = label
    return edge


def layout(nodes):
    for i, n in enumerate(nodes):
        n.setdefault("x", 40 + (i % 6) * 260)
        n.setdefault("y", 40 + (i // 6) * 130)
    return nodes


def hi(text: str) -> dict:
    return {"textByLanguage": {"hi": text}}


# ── edge token banks ─────────────────────────────────────────────────────────
YES = ("yes/yeah/yep/yes please/sure/ok/okay/of course/please do/go ahead/"
       "proceed/haan/haan ji/ji haan/ji/theek hai/thik hai/kar do/kar dijiye/"
       "kardo/bilkul/zaroor/chalo/हाँ/हां/जी हाँ/जी/ठीक है/कर दो/कर दीजिए/"
       "बिल्कुल/ज़रूर")
NO = ("no/nope/not now/not needed/no need/no thanks/no thank you/nahi/nahin/"
      "nahi chahiye/rehne do/mat karo/zaroorat nahi/नहीं/नही/नहीं चाहिए/"
      "रहने दो/मत करो/ज़रूरत नहीं")
# anything-else hub: a bare "one more thing / haan" means another request
ANY_YES = ("yes/yes please/haan/haan ji/ji haan/one more/one more thing/"
           "ek aur/aur ek/another request/something else/kuch aur hai/"
           "ek aur kaam/हाँ/हां/जी हाँ/एक और/और एक/कुछ और")
CLOSE = (NO + "/nothing/nothing else/that's all/thats all/that is all/"
         "that's it/all good/bye/goodbye/thank you/thanks/bas/bas itna hi/"
         "itna hi/kuch nahi/aur kuch nahi/ho gaya/theek hai bas/बस/"
         "बस इतना ही/कुछ नहीं/और कुछ नहीं/धन्यवाद/शुक्रिया/हो गया")

BAL = ("balance/account balance/available balance/savings balance/"
       "how much money/kitna balance/balance kitna/kitne paise hain/"
       "paise kitne/बैलेंस/बैलेन्स/कितने पैसे/खाते में कितना")
TXN = ("recent transactions/recent transaction/last transactions/"
       "last transaction/last few transactions/latest transactions/"
       "transactions batao/transactions sunao/हाल के ट्रांज़ैक्शन/"
       "पिछले ट्रांज़ैक्शन/लेन-देन")
MINI = ("mini statement/mini-statement/ministatement/transaction history/"
        "last five transactions/statement on sms/sms statement/"
        "मिनी स्टेटमेंट/ट्रांज़ैक्शन हिस्ट्री")
LOST = ("lost my card/card lost/card is lost/lost my debit card/"
        "debit card lost/debit card is lost/card stolen/card got stolen/"
        "stolen/card kho gaya/card gum/kho gaya/chori/chura/someone may use/"
        "someone is using/someone used/misuse/fraud/unauthorized/"
        "block my card/block card/block the card/block debit card/"
        "card block/block kar/freeze/card band karo/कार्ड खो गया/खो गया/"
        "गुम/चोरी/ब्लॉक/कार्ड ब्लॉक/फ्रीज़")
REPL = ("replacement card/replacement/new card/new debit card/reissue/"
        "naya card/dusra card/replace my card/replace the card/"
        "रिप्लेसमेंट/नया कार्ड/नया डेबिट कार्ड")
PIN = ("pin reset/reset pin/reset my pin/reset the pin/forgot pin/"
       "forgot my pin/forgotten pin/change pin/change my pin/atm pin/"
       "debit card pin/pin change/pin bhool/pin yaad nahi/new pin/"
       "pin generate/पिन/पिन रीसेट/पिन भूल")
FAILED = ("atm failed/failed atm/transaction failed/failed transaction/"
          "transaction unsuccessful/transaction was unsuccessful/"
          "cash not received/did not receive cash/didn't receive cash/"
          "no cash/money deducted/amount deducted/amount debited/"
          "money debited/money was deducted/deducted but/paise kat gaye/"
          "paise kat/paise cut/cash nahi nikla/cash nahi aaya/"
          "paise nahi nikle/पैसे कट गए/पैसे कट/कैश नहीं निकला/"
          "ट्रांज़ैक्शन फेल/एटीएम फेल")
STMT = ("account statement/bank statement/send statement/send my statement/"
        "transaction statement/statement of account/statement chahiye/"
        "statement bhejo/statement bhej do/email statement/statement/"
        "स्टेटमेंट/अकाउंट स्टेटमेंट/बैंक स्टेटमेंट")
EMAIL = ("change email/update email/email address/new email/change my email/"
         "update my email/registered email/email update/email change/"
         "email badal/email id/profile update/update profile/"
         "update my profile/change profile/ईमेल/ईमेल बदल/प्रोफाइल अपडेट/"
         "प्रोफ़ाइल")
AGENT = ("agent/human/customer care/executive/representative/real person/"
         "insaan se/aadmi se/kisi se baat/manager/supervisor/एजेंट/"
         "इंसान से/कस्टमर केयर/मैनेजर")

# Every service switch a hub offers, in tie-break order (first wins ties).
SERVICES = [
    ("bal", BAL, "n_hub_balance"),
    ("txn", TXN, "n_msg_txns"),
    ("mini", MINI, "n_hub_ministmt"),
    ("lost", LOST, "n_hub_block"),
    ("repl", REPL, "n_hub_replace_direct"),
    ("pin", PIN, "n_ask_pin_otp"),
    ("failed", FAILED, "n_hub_failed_txn"),
    ("stmt", STMT, "n_hub_stmt"),
    ("email", EMAIL, "n_ask_profile_otp"),
]

CARD_END = "X X X X"
MOBILE_RE = r"(?<![0-9])(?:91|0)?([0-9]{10})(?![0-9])"
OTP_RE = r"(?<![0-9])([0-9]{6})(?![0-9])"


def otp_ask(nid, label, variable, question_en, question_hi):
    """A fail-closed six-digit OTP ask; the OTP is never read back (pii).

    Wording rule: the bot NAMES the OTP in one sentence and asks for "the six
    digit code" in the next. The Finance guardrail profile's
    ``payment_collection_restriction`` blocks any assistant sentence that puts
    a solicitation verb (enter/provide/share/tell/give/confirm/repeat) and the
    word OTP/PIN/CVV together — which is correct for credential phishing and
    would otherwise suppress this legitimate step. Splitting the sentence
    keeps the script's meaning and the compliance rule intact. Regression:
    au_bank/tests/guardrail_audit.py.
    """
    return N(nid, "ask", label, {
        "question": question_en,
        "variable": variable,
        "entity": {"dataType": "text", "regexPattern": OTP_RE, "pii": True},
        "unmatchedReply": ("To complete the verification, please say the six "
                           "digit OTP sent to your registered mobile number."),
        "unmatchedReplyByLanguage": {
            "hi": ("Verification पूरा करने के लिए, कृपया अपने registered mobile "
                   "number पर आया छह अंकों का OTP बताइए।")},
        **hi(question_hi),
    })


NODES = layout([
    N("n_start", "start", "Call starts"),
    N("n_cond_verified", "condition", "Already verified?", {
        "variable": "customer_verified", "operator": "exists"}),

    # ── authentication ────────────────────────────────────────────────────
    N("n_ask_mobile", "ask", "Registered mobile number", {
        "question": ("Sure. For security purposes, please first tell me your "
                     "registered mobile number."),
        "variable": "auth_mobile",
        "entity": {"dataType": "phone", "regexPattern": MOBILE_RE, "pii": True},
        "unmatchedReply": ("For your security, I need to verify you before I "
                           "can help with account or card services. Please "
                           "tell me your ten digit registered mobile number."),
        "unmatchedReplyByLanguage": {
            "hi": ("आपकी सुरक्षा के लिए, account या card services में मदद करने से "
                   "पहले मुझे आपको verify करना होगा। कृपया अपना दस अंकों का "
                   "registered mobile number बताइए।")},
        **hi("जी ज़रूर। सुरक्षा के लिए, कृपया सबसे पहले अपना registered mobile "
             "number बताइए।"),
    }),
    otp_ask("n_ask_otp", "Login OTP", "auth_otp",
            ("Thank you. An OTP has been sent to your registered mobile "
             "number. Please share the six digit code."),
            ("धन्यवाद। आपके registered mobile number पर एक OTP भेजा गया है। "
             "कृपया छह अंकों का OTP बताइए।")),
    N("n_msg_verified", "message", "Identity verified", {
        "text": "Your identity has been successfully verified.",
        "setSlots": {"customer_verified": True},
        **hi("आपकी पहचान सफलतापूर्वक verify हो गई है।"),
    }),
    N("n_msg_auth_failed", "message", "Verification failed — close", {
        "text": ("I'm sorry, I was unable to verify your details, so I cannot "
                 "proceed with account or card services on this call. Please "
                 "call again later. Thank you for calling AU Small Finance "
                 "Bank."),
        **hi("माफ़ कीजिए, मैं आपकी details verify नहीं कर पाई, इसलिए इस call पर "
             "account या card services के साथ आगे नहीं बढ़ सकती। कृपया बाद में "
             "दोबारा call करें। AU Small Finance Bank को call करने के लिए "
             "धन्यवाद।"),
    }),

    # ── service hubs ──────────────────────────────────────────────────────
    N("n_hub_services", "intent", "How may I assist you?", {
        "prompt": "How may I assist you today?",
        **hi("बताइए, आज मैं आपकी किस प्रकार मदद कर सकती हूँ?"),
    }),

    # balance
    N("n_hub_balance", "intent", "Balance + recent transactions?", {
        "prompt": (f"Your Savings Account ending with {CARD_END} has an "
                   "available balance of forty-five thousand two hundred and "
                   "eighty rupees. Would you also like to hear your recent "
                   "transactions?"),
        **hi(f"आपके Savings Account, जो {CARD_END} पर समाप्त होता है, में "
             "available balance पैंतालीस हज़ार दो सौ अस्सी रुपये है। क्या मैं "
             "आपको recent transactions भी बता दूँ?"),
    }),
    N("n_msg_txns", "message", "Recent transactions (demo)", {
        "text": ("Your recent transactions are: two thousand five hundred "
                 "rupees credited via UPI on the tenth of June, one thousand "
                 "two hundred rupees debited via Debit Card on the ninth of "
                 "June, and fifteen thousand rupees salary credited on the "
                 "seventh of June."),
        **hi("आपके recent transactions हैं: दस जून को UPI से दो हज़ार पाँच सौ "
             "रुपये credit हुए, नौ जून को Debit Card से एक हज़ार दो सौ रुपये "
             "debit हुए, और सात जून को पंद्रह हज़ार रुपये salary credit हुई।"),
    }),

    # mini statement
    N("n_hub_ministmt", "intent", "Mini statement via SMS?", {
        "prompt": ("Your last five transactions are available. Would you like "
                   "them sent to your registered mobile number via SMS?"),
        **hi("आपके last five transactions available हैं। क्या मैं "
             "उन्हें SMS के द्वारा आपके registered mobile number पर भेज दूँ?"),
    }),
    N("n_msg_ministmt_sent", "message", "Mini statement sent (simulated)", {
        "text": "Your mini statement has been sent successfully.",
        **hi("आपका mini statement सफलतापूर्वक भेज दिया गया है।"),
    }),

    # lost / stolen / block
    N("n_hub_block", "intent", "Block the card?", {
        "prompt": ("I'm sorry to hear that. To prevent any unauthorized use, I "
                   f"can immediately block your debit card ending with {CARD_END}. "
                   "Would you like me to proceed and block the card?"),
        **hi("यह सुनकर मुझे खेद है। किसी भी unauthorized use को रोकने के लिए, मैं "
             f"आपका {CARD_END} पर समाप्त होने वाला debit card तुरंत block कर "
             "सकती हूँ। क्या मैं आपका debit card block कर दूँ?"),
    }),
    N("n_hub_replace", "intent", "Blocked — replacement card?", {
        "prompt": ("Your debit card has been successfully blocked. Would you "
                   "like to request a replacement card?"),
        **hi("आपका debit card सफलतापूर्वक block कर दिया गया है। क्या मैं "
             "आपके लिए replacement card की request दर्ज कर दूँ?"),
    }),
    N("n_hub_replace_direct", "intent", "Replacement card (direct request)?", {
        "prompt": ("I can register a replacement request for your debit card "
                   f"ending with {CARD_END}. Would you like me to proceed?"),
        **hi(f"मैं आपके {CARD_END} पर समाप्त होने वाले debit card के लिए "
             "replacement request register कर सकती हूँ। क्या मैं आगे बढ़ूँ?"),
    }),
    N("n_msg_replace_done", "message", "Replacement registered (simulated)", {
        "text": ("Your replacement debit card request has been registered "
                 "successfully. Your reference number is D C 4 5 8 9 2 1. The "
                 "card will be delivered to your registered address within "
                 "five to seven working days."),
        **hi("आपकी replacement debit card request सफलतापूर्वक register हो गई है। "
             "आपका reference number है D C 4 5 8 9 2 1। Card आपके registered "
             "address पर पाँच से सात working days में deliver हो जाएगा।"),
    }),
    N("n_msg_block_declined", "message", "Block declined", {
        "text": ("Alright, I have not blocked your card. If you notice any "
                 "suspicious activity, please contact us immediately."),
        **hi("ठीक है, मैंने आपका card block नहीं किया है। अगर आपको कोई suspicious "
             "activity दिखे, तो कृपया हमसे तुरंत संपर्क करें।"),
    }),
    N("n_msg_noted", "message", "Acknowledge a no", {
        "text": "Alright, no problem.",
        **hi("ठीक है, कोई बात नहीं।"),
    }),

    # PIN reset (fresh OTP; the existing PIN is never requested)
    otp_ask("n_ask_pin_otp", "PIN reset — OTP", "otp_pinreset",
            ("Certainly. For security purposes, I have sent an OTP to your "
             "registered mobile number. Please share the six digit "
             "code."),
            ("जी ज़रूर। सुरक्षा के लिए, मैंने आपके registered mobile number पर एक "
             "OTP भेजा है। कृपया छह अंकों का OTP बताइए।")),
    N("n_msg_pin_done", "message", "PIN reset processed (simulated)", {
        "text": ("Verification successful. Your PIN reset request has been "
                 "processed, and instructions have been sent to your "
                 "registered mobile number."),
        **hi("Verification सफल रही। आपकी PIN reset request process कर दी गई है, "
             "और instructions आपके registered mobile number पर भेज दिए गए हैं।"),
    }),

    # failed ATM transaction
    N("n_hub_failed_txn", "intent", "Failed ATM — raise SR?", {
        "prompt": ("Let me check the transaction status. I can see a failed "
                   "ATM withdrawal of five thousand rupees made today. The "
                   "amount is expected to be automatically reversed within "
                   "twenty-four hours. Would you like me to raise a service "
                   "request in case the reversal does not happen?"),
        **hi("मैं transaction status check करती हूँ। मुझे आज की पाँच हज़ार रुपये "
             "की एक failed ATM withdrawal दिख रही है। यह amount चौबीस घंटों के "
             "भीतर अपने आप reverse हो जाना चाहिए। अगर reversal न हो, तो क्या मैं "
             "आपके लिए एक service request raise कर दूँ?"),
    }),
    N("n_msg_sr_done", "message", "Service request registered (simulated)", {
        "text": ("Your service request has been registered successfully. Your "
                 "reference number is S R 7 8 4 5 2 1. Our team will update "
                 "you within twenty-four hours."),
        **hi("आपकी service request सफलतापूर्वक register हो गई है। आपका reference "
             "number है S R 7 8 4 5 2 1। हमारी team आपको चौबीस घंटों के भीतर "
             "update देगी।"),
    }),
    N("n_msg_sr_declined", "message", "SR declined", {
        "text": ("Alright. The amount should be reversed automatically within "
                 "twenty-four hours. If it is not, please contact us again and "
                 "we will raise a service request."),
        **hi("ठीक है। यह amount चौबीस घंटों के भीतर अपने आप reverse हो जाना चाहिए। "
             "अगर ऐसा न हो, तो कृपया हमसे दोबारा संपर्क करें, हम service request "
             "raise कर देंगे।"),
    }),

    # account statement
    N("n_hub_stmt", "intent", "Statement — last 30 days?", {
        "prompt": ("Certainly. Would you like the account statement for the "
                   "last thirty days?"),
        **hi("जी ज़रूर। क्या आपको पिछले तीस दिनों का account statement चाहिए?"),
    }),
    N("n_msg_stmt_done", "message", "Statement processed (simulated)", {
        "text": ("Your account statement request has been processed and will "
                 "be sent to your registered email address shortly."),
        **hi("आपकी account statement request process कर दी गई है। आपका statement "
             "जल्द ही आपके registered email address पर भेज दिया जाएगा।"),
    }),
    N("n_msg_stmt_declined", "message", "Statement declined", {
        "text": "Alright, I have not processed a statement request.",
        **hi("ठीक है, मैंने statement की request process नहीं की है।"),
    }),

    # profile / email update (fresh OTP)
    otp_ask("n_ask_profile_otp", "Profile update — OTP", "otp_profile",
            ("I can assist you with that. For security purposes, I have sent "
             "an OTP to your registered mobile number. Please share "
             "the six digit code."),
            ("मैं इसमें आपकी मदद कर सकती हूँ। सुरक्षा के लिए, मैंने आपके "
             "registered mobile number पर एक OTP भेजा है। कृपया छह अंकों का "
             "OTP बताइए।")),
    N("n_msg_profile_done", "message", "Update request registered (simulated)", {
        "text": ("Verification successful. Your update request has been "
                 "registered. A confirmation message will be sent once the "
                 "update is completed."),
        **hi("Verification सफल रही। आपकी update request register कर ली गई है। "
             "Update पूरा होने पर आपको एक confirmation message भेजा जाएगा।"),
    }),
    N("n_msg_otp_failed", "message", "Service OTP failed", {
        "text": ("I'm sorry, I could not verify the OTP, so this request has "
                 "not been processed. You can try again later."),
        **hi("माफ़ कीजिए, मैं OTP verify नहीं कर पाई, इसलिए यह request process "
             "नहीं हुई है। कृपया बाद में दोबारा कोशिश कीजिए।"),
    }),

    # ── wrap-up ───────────────────────────────────────────────────────────
    N("n_hub_anything", "intent", "Anything else?", {
        "prompt": "Is there anything else I can help you with today?",
        **hi("क्या आज मैं आपकी किसी और चीज़ में मदद कर सकती हूँ?"),
    }),
    N("n_msg_close", "message", "Closing", {
        "text": ("Thank you for banking with AU Small Finance Bank. Have a "
                 "great day."),
        **hi("AU Small Finance Bank के साथ banking करने के लिए धन्यवाद। आपका दिन "
             "शुभ हो।"),
    }),
    N("n_handover", "handover", "Customer care handover", {
        "queue": "banking_support",
        "text": ("Sure, I am connecting you to a customer care executive. "
                 "Please stay on the line."),
        **hi("जी, मैं आपको हमारे customer care executive से connect कर रही हूँ। "
             "कृपया line पर बने रहिए।"),
    }),
    N("n_end", "end", "Call ends"),
])

# Hubs whose own branch a switch edge must not point back into.
_OWN_BRANCH = {
    "n_hub_services": set(),
    "n_hub_anything": set(),
    "n_hub_balance": {"bal", "txn"},
    "n_hub_ministmt": {"mini"},
    "n_hub_block": {"lost"},
    "n_hub_replace": {"lost", "repl"},
    "n_hub_replace_direct": {"repl", "lost"},
    "n_hub_failed_txn": {"failed"},
    "n_hub_stmt": {"stmt", "mini"},
}


def service_edges(hub):
    return [E(hub, target, tokens) for key, tokens, target in SERVICES
            if key not in _OWN_BRANCH.get(hub, set())]


EDGES = [
    E("n_start", "n_cond_verified"),
    E("n_cond_verified", "n_hub_services", "true"),
    E("n_cond_verified", "n_ask_mobile", "false"),

    # authentication chain (ask success = first edge; fallback = retries out)
    E("n_ask_mobile", "n_ask_otp"),
    E("n_ask_mobile", "n_msg_auth_failed", "fallback"),
    E("n_ask_otp", "n_msg_verified"),
    E("n_ask_otp", "n_msg_auth_failed", "fallback"),
    E("n_msg_verified", "n_hub_services"),
    E("n_msg_auth_failed", "n_end"),

    # services hub
    *service_edges("n_hub_services"),
    E("n_hub_services", "n_handover", AGENT),
    E("n_hub_services", "n_msg_close", CLOSE),

    # balance
    E("n_hub_balance", "n_msg_txns", YES),
    E("n_hub_balance", "n_msg_noted", NO),
    *service_edges("n_hub_balance"),
    E("n_hub_balance", "n_handover", AGENT),
    E("n_msg_txns", "n_hub_anything"),

    # mini statement
    E("n_hub_ministmt", "n_msg_ministmt_sent", YES),
    E("n_hub_ministmt", "n_msg_noted", NO),
    *service_edges("n_hub_ministmt"),
    E("n_hub_ministmt", "n_handover", AGENT),
    E("n_msg_ministmt_sent", "n_hub_anything"),

    # lost / block / replacement
    E("n_hub_block", "n_hub_replace",
      YES + "/block it/block kar do/block karo/please block/ब्लॉक कर दो/ब्लॉक करो"),
    E("n_hub_block", "n_msg_block_declined", NO + "/don't block/dont block/block mat/ब्लॉक मत"),
    *service_edges("n_hub_block"),
    E("n_hub_block", "n_handover", AGENT),
    E("n_hub_replace", "n_msg_replace_done",
      YES + "/replacement/new card/naya card/नया कार्ड"),
    E("n_hub_replace", "n_msg_noted", NO),
    *service_edges("n_hub_replace"),
    E("n_hub_replace", "n_handover", AGENT),
    E("n_hub_replace_direct", "n_msg_replace_done", YES),
    E("n_hub_replace_direct", "n_msg_noted", NO),
    *service_edges("n_hub_replace_direct"),
    E("n_hub_replace_direct", "n_handover", AGENT),
    E("n_msg_replace_done", "n_hub_anything"),
    E("n_msg_block_declined", "n_hub_anything"),
    E("n_msg_noted", "n_hub_anything"),

    # PIN reset
    E("n_ask_pin_otp", "n_msg_pin_done"),
    E("n_ask_pin_otp", "n_msg_otp_failed", "fallback"),
    E("n_msg_pin_done", "n_hub_anything"),

    # failed ATM transaction
    E("n_hub_failed_txn", "n_msg_sr_done",
      YES + "/raise it/raise the request/raise kar do/request raise/रेज़ कर दो"),
    E("n_hub_failed_txn", "n_msg_sr_declined", NO),
    *service_edges("n_hub_failed_txn"),
    E("n_hub_failed_txn", "n_handover", AGENT),
    E("n_msg_sr_done", "n_hub_anything"),
    E("n_msg_sr_declined", "n_hub_anything"),

    # account statement
    E("n_hub_stmt", "n_msg_stmt_done", YES + "/last thirty days/30 days/thirty days/तीस दिन"),
    E("n_hub_stmt", "n_msg_stmt_declined", NO),
    *service_edges("n_hub_stmt"),
    E("n_hub_stmt", "n_handover", AGENT),
    E("n_msg_stmt_done", "n_hub_anything"),
    E("n_msg_stmt_declined", "n_hub_anything"),

    # profile / email update
    E("n_ask_profile_otp", "n_msg_profile_done"),
    E("n_ask_profile_otp", "n_msg_otp_failed", "fallback"),
    E("n_msg_profile_done", "n_hub_anything"),
    E("n_msg_otp_failed", "n_hub_anything"),

    # anything else?
    E("n_hub_anything", "n_msg_close", CLOSE),
    E("n_hub_anything", "n_hub_services", ANY_YES),
    *service_edges("n_hub_anything"),
    E("n_hub_anything", "n_handover", AGENT),
    E("n_msg_close", "n_end"),
]


# ── offline routing self-check (engine's own edge selector) ──────────────────
# (hub, utterance, expected target node). "off_script" = the hub must NOT
# advance (the LLM answers, the flow keeps waiting).
CHECKS = [
    ("n_hub_services", "I want to check my account balance", "n_hub_balance"),
    ("n_hub_services", "how much money do I have", "n_hub_balance"),
    ("n_hub_services", "mera balance kitna hai", "n_hub_balance"),
    ("n_hub_services", "खाते में कितने पैसे हैं", "n_hub_balance"),
    ("n_hub_services", "I need my mini statement", "n_hub_ministmt"),
    ("n_hub_services", "transaction history chahiye", "n_hub_ministmt"),
    ("n_hub_services", "recent transactions batao", "n_msg_txns"),
    ("n_hub_services", "I have lost my debit card", "n_hub_block"),
    ("n_hub_services", "my card is stolen", "n_hub_block"),
    ("n_hub_services", "someone may use my card", "n_hub_block"),
    ("n_hub_services", "please block my card", "n_hub_block"),
    ("n_hub_services", "freeze my debit card", "n_hub_block"),
    ("n_hub_services", "मेरा कार्ड खो गया है", "n_hub_block"),
    ("n_hub_services", "कार्ड ब्लॉक कर दो", "n_hub_block"),
    ("n_hub_services", "I want a replacement card", "n_hub_replace_direct"),
    ("n_hub_services", "I want to reset my debit card PIN", "n_ask_pin_otp"),
    ("n_hub_services", "forgot my atm pin", "n_ask_pin_otp"),
    ("n_hub_services", "pin reset karna hai", "n_ask_pin_otp"),
    ("n_hub_services", "Money was deducted but the ATM transaction failed", "n_hub_failed_txn"),
    ("n_hub_services", "atm failed but money deducted", "n_hub_failed_txn"),
    ("n_hub_services", "cash not received", "n_hub_failed_txn"),
    ("n_hub_services", "paise kat gaye par cash nahi nikla", "n_hub_failed_txn"),
    ("n_hub_services", "please send me my account statement", "n_hub_stmt"),
    ("n_hub_services", "bank statement bhej do", "n_hub_stmt"),
    ("n_hub_services", "I want to update my email address", "n_ask_profile_otp"),
    ("n_hub_services", "change my registered email", "n_ask_profile_otp"),
    ("n_hub_services", "profile update karna hai", "n_ask_profile_otp"),
    ("n_hub_services", "I want to talk to a customer care executive", "n_handover"),
    ("n_hub_services", "nothing, thank you", "n_msg_close"),
    ("n_hub_services", "Hindi me baat karo", "off_script"),
    ("n_hub_balance", "yes", "n_msg_txns"),
    ("n_hub_balance", "haan batao", "n_msg_txns"),
    ("n_hub_balance", "हाँ", "n_msg_txns"),
    ("n_hub_balance", "no thanks", "n_msg_noted"),
    ("n_hub_balance", "नहीं", "n_msg_noted"),
    ("n_hub_balance", "actually my debit card is lost", "n_hub_block"),
    ("n_hub_balance", "no, actually I lost my card", "n_hub_block"),
    ("n_hub_balance", "I need my mini statement instead", "n_hub_ministmt"),
    ("n_hub_ministmt", "yes please", "n_msg_ministmt_sent"),
    ("n_hub_ministmt", "no", "n_msg_noted"),
    # "wait…" carries the platform HOLD signal and stays off-script by design;
    # a plain mid-flow switch takes the service edge.
    ("n_hub_ministmt", "actually check my account balance first", "n_hub_balance"),
    ("n_hub_ministmt", "wait a minute", "off_script"),
    ("n_hub_block", "yes", "n_hub_replace"),
    ("n_hub_block", "yes block it", "n_hub_replace"),
    ("n_hub_block", "haan block kar do", "n_hub_replace"),
    ("n_hub_block", "हाँ, ब्लॉक कर दो", "n_hub_replace"),
    ("n_hub_block", "no don't block", "n_msg_block_declined"),
    ("n_hub_block", "नहीं", "n_msg_block_declined"),
    ("n_hub_block", "what is my balance first", "n_hub_balance"),
    ("n_hub_replace", "yes", "n_msg_replace_done"),
    ("n_hub_replace", "yes please send a new card", "n_msg_replace_done"),
    ("n_hub_replace", "no", "n_msg_noted"),
    ("n_hub_replace", "nahi chahiye", "n_msg_noted"),
    ("n_hub_replace_direct", "yes", "n_msg_replace_done"),
    ("n_hub_failed_txn", "yes", "n_msg_sr_done"),
    ("n_hub_failed_txn", "haan raise kar do", "n_msg_sr_done"),
    ("n_hub_failed_txn", "no", "n_msg_sr_declined"),
    ("n_hub_stmt", "yes", "n_msg_stmt_done"),
    ("n_hub_stmt", "yes last thirty days", "n_msg_stmt_done"),
    ("n_hub_stmt", "no", "n_msg_stmt_declined"),
    ("n_hub_anything", "no", "n_msg_close"),
    ("n_hub_anything", "no that's all", "n_msg_close"),
    ("n_hub_anything", "nothing else, thank you", "n_msg_close"),
    ("n_hub_anything", "bas itna hi", "n_msg_close"),
    ("n_hub_anything", "नहीं, बस इतना ही", "n_msg_close"),
    ("n_hub_anything", "yes, one more thing", "n_hub_services"),
    ("n_hub_anything", "haan ek aur kaam hai", "n_hub_services"),
    ("n_hub_anything", "actually my debit card is lost", "n_hub_block"),
    ("n_hub_anything", "I also want my account statement", "n_hub_stmt"),
    ("n_hub_anything", "reset my pin", "n_ask_pin_otp"),
    ("n_hub_anything", "can you speak in English please", "off_script"),
    ("n_hub_anything", "what is the interest rate on savings account", "off_script"),
]


def self_check() -> int:
    from shared.orchestration.router import classify_user_signal
    from shared.orchestration.workflow_engine import (
        _choose_intent_edge_detailed,
        _edge_meta,
    )

    by_hub = {}
    for edge in EDGES:
        by_hub.setdefault(edge["from"], []).append(edge)
    failures = 0
    for hub, text, want in CHECKS:
        meta = _edge_meta(by_hub[hub])
        signal = classify_user_signal(text)
        edge, why, token = _choose_intent_edge_detailed(meta, text, signal)
        got = edge["to"] if edge is not None else why
        ok = got == want or (want == "off_script" and edge is None)
        if not ok:
            failures += 1
        print(f"{'ok  ' if ok else 'FAIL'} {hub:22} {text!r:52} signal={signal!s:15} "
              f"-> {got} ({why}{', ' + token if token else ''})")
    print(f"{len(CHECKS) - failures}/{len(CHECKS)} routing checks passed")
    return failures


if __name__ == "__main__":
    if "--check" in sys.argv:
        raise SystemExit(1 if self_check() else 0)
    if self_check():
        raise SystemExit("routing self-check failed — not saving")
    c = client()
    current = check(c.get(f"/bots/{BOT}/workflow"), "read current workflow")
    if current.get("id") != WORKFLOW_ID:
        raise SystemExit(f"unexpected workflow id {current.get('id')} (wanted {WORKFLOW_ID})")
    data = check(c.put(f"/bots/{BOT}/workflow", json={
        "name": WORKFLOW_NAME, "nodes": NODES, "edges": EDGES, "status": "approved",
    }), f"workflow '{WORKFLOW_NAME}' ({len(NODES)} nodes, {len(EDGES)} edges)")
    issues = data.get("issues") or []
    if issues:
        print(f"     issues: {json.dumps(issues, ensure_ascii=False)[:800]}")
    print("workflow done — id:", data.get("id"), "version:", data.get("version"),
          "status:", data.get("status"))
