"""Localized canned phrases — LAST-RESORT technical fallbacks only.

These strings exist for the moments the agentic path cannot run: the LLM
provider is down or timing out, a workflow hit an internal error, or the
call is being torn down and there is no time for a generation round trip.
They are NOT the normal conversation path — identity re-asks, redirects,
clarifications and outcome statements are normally generated per turn from
the bot's own prompt/goal policy, in the caller's language.

Every fixed phrase the voice runtime or workflow engine can speak WITHOUT the
LLM must go through :func:`canned` so a Hindi caller never hears an English
fallback mid-conversation. Locale resolution is by base language code
("hi-IN" → "hi"); English is the final fallback for languages without an
entry. Hinglish callers are Hindi callers here — Devanagari text is what the
hi-IN TTS voices speak naturally.

This table is deliberately domain-neutral: platform mechanics only (errors,
handoffs, hang-ups, workflow retries). Domain-specific fallbacks live with
their domain policy (e.g. voice_runtime.call_policy for collections), never
in shared orchestration code.

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
    # A caller dictating a numeric identifier paused partway through; the
    # digits heard so far are held and the ask stays open for the rest.
    "wf_digits_partial": {
        "en": "Okay, I have noted the digits so far — please continue.",
        "hi": "जी, अब तक के अंक नोट कर लिए — कृपया आगे बताइए।",
    },
    # Count-bearing variant ({count} substituted by the engine): a concise,
    # informative acknowledgement after a genuinely long dictation pause.
    "wf_digits_partial_count": {
        "en": "I have noted {count} digits so far — please continue.",
        "hi": "अब तक {count} अंक नोट कर लिए हैं — कृपया आगे बताइए।",
    },
    # Caller explicitly restarted the identifier ("start again", "phir se").
    "wf_digits_restart": {
        "en": "Okay, let's start over — please tell me the complete number again.",
        "hi": "ठीक है, फिर से शुरू करते हैं — कृपया पूरा नंबर दोबारा बताइए।",
    },
    # Caller asked what was captured but nothing is buffered yet.
    "wf_digits_none": {
        "en": "I haven't noted any digits yet — please tell me the number.",
        "hi": "अभी तक कोई अंक नोट नहीं हुआ है — कृपया नंबर बताइए।",
    },
    # Caller asked what was captured ({count}/{digits} substituted).
    "wf_digits_readback": {
        "en": "So far I have noted {count} digits: {digits}. Please continue, "
              "or say 'start again' to restart.",
        "hi": "अब तक {count} अंक नोट किए हैं: {digits}। कृपया आगे बताइए, या "
              "'फिर से' बोलकर दोबारा शुरू कीजिए।",
    },
    # Masked variant for sensitive identifiers (phone numbers etc.).
    "wf_digits_readback_masked": {
        "en": "So far I have noted {count} digits, ending in {digits}. Please "
              "continue, or say 'start again' to restart.",
        "hi": "अब तक {count} अंक नोट किए हैं, आख़िर में {digits}। कृपया आगे "
              "बताइए, या 'फिर से' बोलकर दोबारा शुरू कीजिए।",
    },
    # The buffered digits exceed every length this identifier can take: the
    # impossible buffer is dropped (a separately-plausible fresh chunk is
    # kept) and the caller is told once what to repeat.
    "wf_digits_overflow": {
        "en": "That has more digits than this number can have — let's start "
              "over. Please tell me the complete number once again.",
        "hi": "इसमें अंक ज़्यादा हो गए हैं — फिर से शुरू करते हैं। कृपया पूरा "
              "नंबर एक बार फिर बताइए।",
    },
    "wf_repeat": {
        "en": "Could you repeat that?",
        "hi": "कृपया दोबारा बोलिए?",
    },
    # An intent node whose author left the prompt empty still needs to ask
    # SOMETHING; and a turn that consumed input while producing no authored
    # reply needs a neutral continuation. Both were hardcoded English before.
    "wf_how_help": {
        "en": "How can I help you today?",
        "hi": "बताइए, मैं आपकी क्या मदद कर सकता हूँ?",
    },
    "wf_anything_else": {
        "en": "Is there anything else I can help you with?",
        "hi": "क्या मैं आपकी किसी और चीज़ में मदद कर सकता हूँ?",
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


def resolve_phrase(
    table: dict[str, dict[str, str]], key: str, locale: str | None = None
) -> str:
    """Locale resolution shared by this table and domain-owned fallback tables.

    ``locale`` is a platform locale ("hi-IN") or bare base code ("hi");
    unknown languages and missing translations fall back to English.
    """
    entry = table.get(key)
    if not entry:
        return ""
    base = (locale or "en").split("-")[0].lower()
    return entry.get(base) or entry.get("en", "")


def canned(key: str, locale: str | None = None) -> str:
    """The canned phrase for ``key`` in the caller's current language."""
    return resolve_phrase(_PHRASES, key, locale)
