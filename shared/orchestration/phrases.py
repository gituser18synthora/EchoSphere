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
            "समझ गया — यह नंबर डू-नॉट-कॉल सूची में डाल दिया जाएगा और आपको "
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
