"""Localized canned phrases for runtime fallbacks.

Every fixed phrase the voice runtime or workflow engine can speak WITHOUT the
LLM (clarifications, safety warnings, hang-up acknowledgements, workflow retry
prefixes, …) must go through :func:`canned` so a Hindi caller never hears an
English fallback mid-conversation. Locale resolution is by base language code
("hi-IN" → "hi"); English is the final fallback for languages without an
entry. Hinglish callers are Hindi callers here — Devanagari text is what the
hi-IN TTS voices speak naturally.

English values are byte-for-byte the strings the runtime used before
localization, so transcripts/tests keyed on them keep working.
"""

_PHRASES: dict[str, dict[str, str]] = {
    "clarify": {
        "en": "Sorry, could you tell me a bit more about what you need?",
        "hi": "माफ़ कीजिए, थोड़ा और बताइए कि आपको क्या चाहिए?",
    },
    "safety": {
        "en": (
            "For your security, please never share card numbers, OTPs or "
            "passwords on this call. How else can I help you?"
        ),
        "hi": (
            "आपकी सुरक्षा के लिए, कृपया कार्ड नंबर, OTP या पासवर्ड इस कॉल पर "
            "कभी न बताएं। बताइए, और क्या मदद करूँ?"
        ),
    },
    "error": {
        "en": "I'm sorry, something went wrong on my end. Could you say that again?",
        "hi": "माफ़ कीजिए, कुछ गड़बड़ हो गई। कृपया दोबारा बोलिए।",
    },
    "kb_miss": {
        "en": (
            "I couldn't find that in the information I have. "
            "Would you like me to connect you with a human agent?"
        ),
        "hi": (
            "माफ़ कीजिए, यह जानकारी मेरे पास नहीं है। "
            "क्या आपको हमारे एजेंट से बात करनी है?"
        ),
    },
    "handoff": {
        "en": "I understand — let me connect you with a human agent. Please hold on.",
        "hi": "ठीक है, आपको हमारे एजेंट से जोड़ा जा रहा है। कृपया लाइन पर बने रहिए।",
    },
    # Deliberately short: it plays between the hang-up request and the actual
    # disconnect, so every extra word delays the caller's goodbye.
    "hangup_ack": {
        "en": "Alright, ending the call now. Goodbye!",
        "hi": "ठीक है, कॉल बंद की जा रही है। धन्यवाद।",
    },
    "dnc_ack": {
        "en": (
            "Understood — this number will be marked do-not-call and you "
            "won't be contacted again. Goodbye."
        ),
        "hi": (
            "ठीक है — यह नंबर डू-नॉट-कॉल सूची में डाल दिया जाएगा और आपको "
            "दोबारा कॉल नहीं की जाएगी। धन्यवाद।"
        ),
    },
    "repeat_none": {
        "en": "I haven't said anything yet.",
        "hi": "अभी तक मैंने कुछ नहीं कहा है।",
    },
    "slower_ack": {
        "en": "Of course, I'll slow down. What would you like to know?",
        "hi": "ठीक है, अब धीरे बताते हैं। बताइए, आपको क्या जानना है?",
    },
    "ack": {
        "en": "Alright.",
        "hi": "ठीक है।",
    },
    # Workflow-engine (definition interpreter) generic strings:
    "wf_retry_prefix": {
        "en": "Sorry, I didn't catch that. ",
        "hi": "माफ़ कीजिए, समझ नहीं आया। ",
    },
    "wf_more_detail": {
        "en": "Could you tell me a bit more?",
        "hi": "थोड़ा और बताइए?",
    },
    "wf_repeat": {
        "en": "Could you repeat that?",
        "hi": "कृपया दोबारा बोलिए?",
    },
    "wf_kb_miss": {
        "en": "I couldn't find that in the information I have.",
        "hi": "माफ़ कीजिए, यह जानकारी मेरे पास नहीं है।",
    },
    "wf_handover": {
        "en": (
            "I'm having trouble capturing that. Let me connect "
            "you with an agent."
        ),
        "hi": (
            "माफ़ कीजिए, बात समझ नहीं पा रहे हैं। "
            "आपको हमारे एजेंट से जोड़ा जा रहा है।"
        ),
    },
    "wf_error": {
        "en": "Something went wrong with this flow. Let me connect you with an agent.",
        "hi": "इस प्रक्रिया में कुछ गड़बड़ हो गई। आपको हमारे एजेंट से जोड़ा जा रहा है।",
    },
    "wf_missing": {
        "en": (
            "I'm sorry — I can't start that flow right now. "
            "Let me connect you with an agent."
        ),
        "hi": (
            "माफ़ कीजिए, यह प्रक्रिया अभी शुरू नहीं हो पा रही है। "
            "आपको हमारे एजेंट से जोड़ा जा रहा है।"
        ),
    },
    "wf_timeout": {
        "en": (
            "I'm sorry, that took longer than expected. "
            "Let me connect you with an agent."
        ),
        "hi": (
            "माफ़ कीजिए, इसमें उम्मीद से ज़्यादा समय लग गया। "
            "आपको हमारे एजेंट से जोड़ा जा रहा है।"
        ),
    },
    # Collections: the opener spoken the moment identity is confirmed. Its
    # content is fully determined by the verified account facts, so it is
    # scripted rather than generated — that removes an LLM round trip from
    # the one turn in the call where the caller has just said a single word
    # and expects an immediate answer. {amount}/{days} are filled by the
    # policy from context; first-person grammar is gendered by the speaking
    # voice in ConversationBrain._say.
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
    # Same turn, when the API gave an amount but no usable due date.
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
    # Identity answer was unclear/partial/noise: re-ask, never assume. The
    # {name} is the customer on record; grammar is gendered by the speaking
    # voice downstream.
    "collections_identity_reask": {
        "en": "Sorry — am I speaking with {name}?",
        "hi": "माफ़ कीजिए, क्या मैं {name} जी से बात कर रहा हूँ?",
    },
    # Identity could not be verified after repeated attempts: close with no
    # account disclosure of any kind.
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
    # The payment-already-made flow: ask for the ACTUAL transaction number.
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
    # Verification outcomes — the ONLY sentences that state a result, each
    # scripted from the tool's answer. {reference} is pre-spaced digit by
    # digit for the TTS.
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
    # A reference was captured but no verification tool exists on this call:
    # verification is honestly PENDING — never claimed done.
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
    # The customer could not provide any reference after clarification.
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
    """The canned phrase for ``key`` in the caller's current language.

    ``locale`` is a platform locale ("hi-IN") or bare base code ("hi");
    unknown languages and missing translations fall back to English.
    """
    table = _PHRASES.get(key)
    if not table:
        return ""
    base = (locale or "en").split("-")[0].lower()
    return table.get(base) or table.get("en", "")
