"""Stage 04 — AU Small Finance Bank router intents (+ the one entity they need).

The bot had NO intents before this stage (verified: GET /bots/{id}/intents
returned []), so every intent here is new — nothing existing was duplicated or
renamed.

Routing: business intents route by WORKFLOW ID (rename-safe; a slug route
would break if the workflow is renamed). Four intents route NOWHERE on
purpose — they are semantic categories the classifier and the reports use,
while the guided flow's own yes/no, closing and service edges do the work:
  confirmation_yes / rejection_no  — every confirmation step is a workflow hub
  conversation_closure             — the "anything else?" hub owns the close
  language_change                  — handled by the runtime language layer and
                                     the prompt; it must never move the flow

``registered_mobile_number`` is the only entity created. It exists so the
router's identifier path (``TurnRouter._match_identifier_workflow``) can start
the flow when the caller answers the greeting with a bare ten-digit number —
without it a lone number is "too short" and lands in a clarification. It is a
format definition, not customer data: no sample value is stored in it. OTP
entities are deliberately NOT created (the platform forbids entities named
after authentication secrets); the workflow's OTP asks carry an inline
six-digit pattern on the node itself.

Run: env/bin/python au_bank/setup/04_intents.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import BOT, TENANT, WORKFLOW_ID, check, client  # noqa: E402

WF = f"workflow:{WORKFLOW_ID}"

ENTITIES = [
    {"name": "registered_mobile_number",
     "description": ("The customer's registered ten-digit mobile number, used "
                     "for call authentication. Format only — no stored value."),
     "kind": "regex", "dataType": "phone",
     "regexPattern": r"(?<![0-9])(?:91|0)?([0-9]{10})(?![0-9])",
     "languages": ["en-IN", "hi-IN"],
     "pii": True, "maskingEnabled": True, "requireConfirmation": False},
]

INTENTS = [
    # ── authentication ────────────────────────────────────────────────────
    {"name": "authentication_otp", "category": "authentication",
     "description": ("Customer gives their registered mobile number or the "
                     "OTP, or asks about the verification step — enters the "
                     "flow's authentication gate."),
     "samples": [
         "my registered mobile number is 9876543210",
         "here is my mobile number", "my mobile number", "this is my number",
         "I want to verify my identity", "verify me", "I did not get the OTP",
         "resend the OTP", "the OTP has not come", "my OTP is 123456",
         "mera registered mobile number hai", "mera mobile number hai",
         "OTP nahi aaya", "OTP dobara bhejo", "verify kar lijiye",
         "मेरा registered mobile number है", "मेरा मोबाइल नंबर है",
         "OTP नहीं आया", "OTP दोबारा भेजिए", "मुझे verify कीजिए"],
     "confidenceThreshold": 0.5, "route": WF, "workflowId": WORKFLOW_ID,
     "optionalEntities": ["registered_mobile_number"], "priority": 50},

    # ── balance & transactions ────────────────────────────────────────────
    {"name": "account_balance", "category": "account_services",
     "description": "Customer asks for the account / available / savings balance.",
     "samples": [
         "check balance", "check my balance", "account balance",
         "what is my account balance", "available balance", "savings balance",
         "how much money do I have", "how much balance is left",
         "tell me my balance", "I want to check my account balance",
         "balance check karna hai", "mera balance kitna hai",
         "account mein kitne paise hain", "balance bataiye",
         "kitna balance bacha hai",
         "बैलेंस बताइए", "मेरा बैलेंस कितना है", "खाते में कितने पैसे हैं",
         "अकाउंट बैलेंस चेक करना है", "उपलब्ध बैलेंस कितना है"],
     "confidenceThreshold": 0.5, "route": WF, "workflowId": WORKFLOW_ID},

    {"name": "recent_transactions", "category": "account_services",
     "description": ("Customer asks to hear the recent transactions (the "
                     "follow-up after a balance enquiry, or asked directly)."),
     "samples": [
         "recent transactions", "my recent transactions",
         "tell me my recent transactions", "what are my last transactions",
         "read out my recent transactions", "latest transactions",
         "recent transactions batao", "pichhle transactions bataiye",
         "haal ke transactions sunao", "last transactions dikhao",
         "हाल के ट्रांज़ैक्शन बताइए", "पिछले लेन-देन बताइए",
         "मेरे recent transactions सुनाइए", "आख़िरी ट्रांज़ैक्शन क्या थे"],
     "confidenceThreshold": 0.5, "route": WF, "workflowId": WORKFLOW_ID},

    {"name": "mini_statement", "category": "account_services",
     "description": ("Customer asks for a mini statement / transaction "
                     "history / the last five transactions on SMS."),
     "samples": [
         "mini statement", "I need my mini statement",
         "send me a mini statement", "mini statement chahiye",
         "transaction history", "I want my transaction history",
         "last five transactions", "send the last five transactions",
         "mini statement bhej do", "transaction history chahiye",
         "pichhle paanch transactions bhejiye",
         "मिनी स्टेटमेंट चाहिए", "मिनी स्टेटमेंट भेज दीजिए",
         "ट्रांज़ैक्शन हिस्ट्री चाहिए", "पिछले पाँच ट्रांज़ैक्शन भेजिए"],
     "confidenceThreshold": 0.5, "route": WF, "workflowId": WORKFLOW_ID},

    # ── debit card ────────────────────────────────────────────────────────
    {"name": "debit_card_lost_stolen", "category": "debit_card",
     "description": ("URGENT — customer reports a lost, stolen or possibly "
                     "misused debit card, or asks for it to be blocked or "
                     "frozen. The flow offers to block and asks for explicit "
                     "confirmation before blocking."),
     "samples": [
         "lost my card", "I lost my card", "I have lost my debit card",
         "debit card lost", "my debit card is lost", "card stolen",
         "my card has been stolen", "someone stole my card",
         "someone may use my card", "someone is using my card",
         "block my card", "please block my card", "block my debit card",
         "freeze my debit card", "freeze my card", "I want to block my card",
         "mera card kho gaya", "mera debit card kho gaya", "card gum ho gaya",
         "card chori ho gaya", "card block kar do", "card band kar do",
         "koi mera card use kar sakta hai",
         "मेरा कार्ड खो गया है", "मेरा डेबिट कार्ड खो गया",
         "मेरा कार्ड चोरी हो गया", "कार्ड ब्लॉक कर दीजिए",
         "डेबिट कार्ड ब्लॉक करना है", "कार्ड फ्रीज़ कर दीजिए",
         "कोई मेरा कार्ड इस्तेमाल कर सकता है"],
     "confidenceThreshold": 0.45, "route": WF, "workflowId": WORKFLOW_ID,
     "priority": 10},

    {"name": "debit_card_block_confirmation", "category": "debit_card",
     "description": ("Customer explicitly CONFIRMS the card block the bot "
                     "offered. A card is never blocked without this."),
     "samples": [
         "yes block it", "yes please block it", "yes block my card",
         "go ahead and block the card", "please proceed and block it",
         "confirm the block", "block it now", "yes I confirm the block",
         "haan block kar do", "haan card block kar dijiye",
         "ji haan block kar do", "aage badhiye block kar dijiye",
         "हाँ ब्लॉक कर दीजिए", "जी हाँ कार्ड ब्लॉक कर दो",
         "हाँ आगे बढ़िए और ब्लॉक कर दीजिए"],
     "confidenceThreshold": 0.5, "route": WF, "workflowId": WORKFLOW_ID},

    {"name": "replacement_debit_card", "category": "debit_card",
     "description": ("Customer wants a replacement / reissued debit card "
                     "after a block, or asks for a new card directly."),
     "samples": [
         "replacement card", "I want a replacement card",
         "send me a replacement debit card", "issue a new debit card",
         "I need a new card", "reissue my card", "replace my debit card",
         "naya card chahiye", "replacement card chahiye",
         "naya debit card bhej dijiye", "dusra card issue kar dijiye",
         "नया कार्ड चाहिए", "रिप्लेसमेंट कार्ड चाहिए",
         "नया डेबिट कार्ड भेज दीजिए", "दूसरा कार्ड जारी कर दीजिए"],
     "confidenceThreshold": 0.5, "route": WF, "workflowId": WORKFLOW_ID},

    {"name": "debit_card_pin_reset", "category": "debit_card",
     "description": ("Customer wants to reset / change / regenerate the debit "
                     "card or ATM PIN. The bot NEVER asks for the existing PIN."),
     "samples": [
         "reset debit card PIN", "I want to reset my debit card PIN",
         "reset my PIN", "forgot PIN", "I forgot my PIN",
         "I have forgotten my ATM PIN", "change PIN", "change my debit card PIN",
         "ATM PIN reset", "generate a new PIN",
         "PIN reset karna hai", "debit card ka PIN reset karna hai",
         "PIN bhool gaya hoon", "ATM PIN yaad nahi hai", "PIN change karna hai",
         "पिन रीसेट करना है", "डेबिट कार्ड का पिन बदलना है",
         "मैं अपना पिन भूल गया हूँ", "एटीएम पिन याद नहीं है",
         "नया पिन बनवाना है"],
     "confidenceThreshold": 0.5, "route": WF, "workflowId": WORKFLOW_ID},

    # ── transactions & statements ─────────────────────────────────────────
    {"name": "failed_atm_transaction", "category": "transaction_services",
     "description": ("Customer reports a failed ATM / card transaction where "
                     "the amount was debited — the flow explains the automatic "
                     "reversal and offers a service request."),
     "samples": [
         "ATM failed but money deducted", "money was deducted but the ATM transaction failed",
         "cash not received", "I did not receive the cash",
         "transaction failed", "my transaction was unsuccessful",
         "amount debited but transaction failed", "money deducted but no cash",
         "the ATM did not give me money", "amount deducted from my account",
         "paise kat gaye lekin cash nahi mila", "ATM se cash nahi nikla",
         "transaction fail ho gaya paise kat gaye", "paise debit ho gaye cash nahi aaya",
         "पैसे कट गए लेकिन कैश नहीं मिला", "एटीएम से पैसे नहीं निकले",
         "ट्रांज़ैक्शन फेल हो गया और पैसे कट गए",
         "खाते से पैसे कट गए पर कैश नहीं आया"],
     "confidenceThreshold": 0.45, "route": WF, "workflowId": WORKFLOW_ID,
     "priority": 20},

    {"name": "account_statement", "category": "account_services",
     "description": ("Customer asks for an account / bank statement to be "
                     "sent (the flow offers the last thirty days by email)."),
     "samples": [
         "account statement", "please send me my account statement",
         "I need my account statement", "bank statement",
         "send me my bank statement", "send statement",
         "transaction statement", "email me my statement",
         "statement of my account", "last thirty days statement",
         "account statement chahiye", "bank statement bhej do",
         "statement bhej dijiye", "mujhe statement chahiye",
         "अकाउंट स्टेटमेंट चाहिए", "बैंक स्टेटमेंट भेज दीजिए",
         "मुझे स्टेटमेंट चाहिए", "पिछले तीस दिन का स्टेटमेंट भेजिए"],
     "confidenceThreshold": 0.5, "route": WF, "workflowId": WORKFLOW_ID},

    {"name": "profile_email_update", "category": "profile_services",
     "description": ("Customer wants to change or update the registered email "
                     "address or other profile details — OTP verified, then "
                     "registered as a request (not applied on the call)."),
     "samples": [
         "change email", "I want to change my email",
         "update email address", "I want to update my email address",
         "change my registered email", "update my registered email id",
         "profile update", "I want to update my profile",
         "change my email id", "update my contact details",
         "email address badalna hai", "email update karna hai",
         "registered email change karna hai", "profile update karna hai",
         "ईमेल पता बदलना है", "ईमेल अपडेट करना है",
         "रजिस्टर्ड ईमेल बदलना है", "प्रोफ़ाइल अपडेट करनी है"],
     "confidenceThreshold": 0.5, "route": WF, "workflowId": WORKFLOW_ID},

    # ── conversation control (no route — the flow's own edges decide) ─────
    {"name": "confirmation_yes", "category": "conversation_control",
     "description": ("A plain affirmative answer to whatever the bot just "
                     "asked. Routed nowhere: the workflow hub the caller is "
                     "standing on owns the yes branch."),
     "samples": [
         "yes", "yes please", "yeah", "sure", "okay", "ok please",
         "of course", "please do", "go ahead", "proceed", "that is correct",
         "haan", "haan ji", "ji haan", "theek hai", "bilkul", "zaroor",
         "kar dijiye", "aage badhiye",
         "हाँ", "हाँ जी", "जी हाँ", "ठीक है", "बिल्कुल", "ज़रूर",
         "कर दीजिए", "आगे बढ़िए"],
     "confidenceThreshold": 0.6, "route": "", "priority": 200},

    {"name": "rejection_no", "category": "conversation_control",
     "description": ("A plain negative answer to whatever the bot just asked "
                     "(including declining a block, a replacement or a service "
                     "request). Routed nowhere: the hub owns the no branch."),
     "samples": [
         "no", "no thanks", "no thank you", "not now", "not needed",
         "no I don't want that", "please don't", "leave it", "cancel that",
         "nahi", "nahi ji", "nahi chahiye", "rehne dijiye", "mat kijiye",
         "abhi nahi",
         "नहीं", "नहीं जी", "नहीं चाहिए", "रहने दीजिए", "मत कीजिए",
         "अभी नहीं"],
     "confidenceThreshold": 0.6, "route": "", "priority": 200},

    {"name": "another_request", "category": "conversation_control",
     "description": ("Customer has a further request after one was completed "
                     "— re-enters the flow's service hub WITHOUT repeating "
                     "authentication."),
     "samples": [
         "I have another request", "one more thing", "one more question",
         "I need help with something else", "there is something else",
         "can you help me with another thing", "yes one more thing",
         "ek aur kaam hai", "ek aur baat", "kuch aur bhi chahiye",
         "ek aur sawaal hai", "haan ek aur cheez",
         "एक और काम है", "एक और सवाल है", "कुछ और भी चाहिए",
         "हाँ एक और बात है"],
     "confidenceThreshold": 0.5, "route": WF, "workflowId": WORKFLOW_ID},

    {"name": "conversation_closure", "category": "conversation_control",
     "description": ("Customer is finished and wants to end the call. Routed "
                     "nowhere: the 'anything else?' hub speaks the AU closing "
                     "line and ends the flow, and the platform's own hang-up "
                     "detection still applies."),
     "samples": [
         "no that's all", "that's all", "that is all for now", "nothing else",
         "nothing more", "I am done", "that's it thank you",
         "no more questions", "thank you that will be all", "goodbye",
         "bas itna hi", "bas yahi tha", "aur kuch nahi", "kuch nahi chahiye",
         "ho gaya dhanyavaad", "bas shukriya",
         "बस इतना ही", "और कुछ नहीं", "कुछ नहीं चाहिए", "बस धन्यवाद",
         "हो गया शुक्रिया", "अब कुछ नहीं चाहिए"],
     "confidenceThreshold": 0.55, "route": "", "priority": 150},

    {"name": "language_change", "category": "conversation_control",
     "description": ("Customer asks to continue in another language. Routed "
                     "NOWHERE by design: the runtime language layer switches "
                     "the reply language and the flow keeps its exact state — "
                     "authentication, the active request and everything "
                     "already collected are preserved."),
     "samples": [
         "can you speak in English", "please speak in English",
         "talk to me in English", "switch to English", "English please",
         "can you speak in Hindi", "please speak in Hindi",
         "switch to Hindi", "Hindi please",
         "Hindi mein baat kijiye", "Hindi mein boliye",
         "English mein baat kijiye", "aap Hindi bol sakti hain",
         "mujhe Hindi mein samjhaiye",
         "हिंदी में बात कीजिए", "हिंदी में बताइए", "अंग्रेज़ी में बात कीजिए",
         "क्या आप हिंदी बोल सकती हैं", "मुझे हिंदी में समझाइए"],
     "confidenceThreshold": 0.5, "route": "", "priority": 120},

    # ── human handover (the flow has AGENT edges on every hub) ────────────
    {"name": "human_agent_request", "category": "conversation_control",
     "description": "Customer explicitly wants a human customer care executive.",
     "samples": [
         "I want to talk to a customer care executive",
         "connect me to a human", "transfer me to an agent",
         "I want to speak to a real person", "let me talk to someone",
         "customer care se baat karao", "kisi executive se baat karani hai",
         "kisi insaan se baat karao",
         "कस्टमर केयर से बात कराइए", "किसी एजेंट से बात करनी है",
         "किसी इंसान से बात कराइए"],
     "confidenceThreshold": 0.7, "route": "handoff", "handoffEnabled": True},
]


if __name__ == "__main__":
    c = client()

    existing_entities = {
        e["name"]: e["id"]
        for e in check(c.get(f"/entities?tenantId={TENANT}"), "list entities")
    }
    for entity in ENTITIES:
        if entity["name"] in existing_entities:
            check(c.patch(f"/entities/{existing_entities[entity['name']]}", json=entity),
                  f"update entity {entity['name']}")
        else:
            check(c.post("/entities", json={**entity, "tenantId": TENANT}),
                  f"entity {entity['name']}")

    existing = {i["name"]: i["id"]
                for i in check(c.get(f"/bots/{BOT}/intents"), "list intents")}
    print(f"     {len(existing)} intent(s) already on the bot: "
          f"{sorted(existing) or 'none'}")
    for intent in INTENTS:
        if intent["name"] in existing:
            check(c.patch(f"/intents/{existing[intent['name']]}", json=intent),
                  f"update intent {intent['name']}")
        else:
            check(c.post(f"/bots/{BOT}/intents", json=intent),
                  f"intent {intent['name']}")
    print(f"intents done — {len(INTENTS)} configured")
