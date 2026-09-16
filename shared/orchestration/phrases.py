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
    # No-response ladder (voice_runtime.silence_policy): rotating check-ins,
    # then a polite close. Authored phrases pass the speaker-grammar adapter,
    # so करता/करती follows the bot's voice gender.
    "silence_check_1": {
        "en": "Hello, can you hear me?",
        "hi": "Hello, क्या आप मुझे सुन पा रहे हैं?",
    },
    "silence_check_2": {
        "en": "Hello, are you still there?",
        "hi": "Hello, क्या आप वहाँ हैं?",
    },
    "silence_check_3": {
        "en": "Are you still on the line? I am here whenever you are ready.",
        "hi": "क्या आप अभी भी line पर हैं? आप बोलिए, मैं यहीं हूँ।",
    },
    "silence_close": {
        "en": (
            "It seems you are unable to talk right now. I will connect with "
            "you later. Goodbye!"
        ),
        "hi": (
            "लगता है अभी आप बात नहीं कर पा रहे हैं। मैं आपसे बाद में connect "
            "करता हूँ। धन्यवाद!"
        ),
    },
    # The caller asked the bot to wait a moment ("ek minute ruko", "hold on").
    "hold_ack": {
        "en": "Sure, I am on the line. Take your time.",
        "hi": "जी बिल्कुल, मैं line पर हूँ।",
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

# Malayalam (ml) and Tamil (ta) renderings of every canned phrase. A second
# table keeps the primary one readable; it is merged at import time so
# resolve_phrase() stays a plain locale lookup — no per-language branches.
# Placeholders ({count}, {digits}) are kept verbatim for the engine to fill.
_PHRASES_ML_TA: dict[str, dict[str, str]] = {
    "clarify": {
        "ml": "ക്ഷമിക്കണം, നിങ്ങൾക്ക് എന്താണ് വേണ്ടതെന്ന് കുറച്ചുകൂടി വിശദമായി പറയാമോ?",
        "ta": "மன்னிக்கவும், உங்களுக்கு என்ன வேண்டும் என்று இன்னும் கொஞ்சம் விளக்கமாகச் சொல்ல முடியுமா?",
    },
    "safety": {
        "ml": ("നിങ്ങളുടെ സുരക്ഷയ്ക്കായി, ഈ കോളിൽ കാർഡ് നമ്പർ, OTP അല്ലെങ്കിൽ പാസ്‌വേഡ് "
               "ഒരിക്കലും പറയരുത്. മറ്റെന്തെങ്കിലും സഹായം വേണോ?"),
        "ta": ("உங்கள் பாதுகாப்பிற்காக, இந்த அழைப்பில் கார்டு எண், OTP அல்லது கடவுச்சொல்லை "
               "ஒருபோதும் சொல்ல வேண்டாம். வேறு என்ன உதவி வேண்டும்?"),
    },
    "error": {
        "ml": "ക്ഷമിക്കണം, എന്റെ ഭാഗത്ത് ഒരു പിശക് സംഭവിച്ചു. ദയവായി ഒന്നുകൂടി പറയാമോ?",
        "ta": "மன்னிக்கவும், என் பக்கம் ஒரு பிழை ஏற்பட்டது. தயவுசெய்து மீண்டும் சொல்ல முடியுமா?",
    },
    "kb_miss": {
        "ml": "ക്ഷമിക്കണം, ആ വിവരം എന്റെ പക്കൽ ഇല്ല. നിങ്ങളെ ഞങ്ങളുടെ ഏജന്റുമായി ബന്ധിപ്പിക്കണോ?",
        "ta": "மன்னிக்கவும், அந்தத் தகவல் என்னிடம் இல்லை. உங்களை எங்கள் ஏஜென்டுடன் இணைக்கவா?",
    },
    "handoff": {
        "ml": "ശരി, നിങ്ങളെ ഞങ്ങളുടെ ഏജന്റുമായി ബന്ധിപ്പിക്കുകയാണ്. ദയവായി ലൈനിൽ തുടരുക.",
        "ta": "சரி, உங்களை எங்கள் ஏஜென்டுடன் இணைக்கிறேன். தயவுசெய்து இணைப்பில் இருங்கள்.",
    },
    "hangup_ack": {
        "ml": "ശരി, കോൾ ഇപ്പോൾ അവസാനിപ്പിക്കുന്നു. നന്ദി.",
        "ta": "சரி, அழைப்பை இப்போது முடிக்கிறேன். நன்றி.",
    },
    "dnc_ack": {
        "ml": "ശരി — ഈ നമ്പർ ഡു-നോട്ട്-കോൾ ലിസ്റ്റിൽ ചേർക്കും, ഇനി നിങ്ങളെ വിളിക്കില്ല. നന്ദി.",
        "ta": "சரி — இந்த எண் டு-நாட்-கால் பட்டியலில் சேர்க்கப்படும், இனி உங்களை அழைக்க மாட்டோம். நன்றி.",
    },
    "silence_check_1": {
        "ml": "ഹലോ, എന്നെ കേൾക്കാൻ കഴിയുന്നുണ്ടോ?",
        "ta": "ஹலோ, நான் பேசுவது கேட்கிறதா?",
    },
    "silence_check_2": {
        "ml": "ഹലോ, നിങ്ങൾ അവിടെയുണ്ടോ?",
        "ta": "ஹலோ, நீங்கள் அங்கே இருக்கிறீர்களா?",
    },
    "silence_check_3": {
        "ml": "നിങ്ങൾ ഇപ്പോഴും ലൈനിൽ ഉണ്ടോ? നിങ്ങൾ തയ്യാറാകുമ്പോൾ പറയൂ, ഞാൻ ഇവിടെയുണ്ട്.",
        "ta": "நீங்கள் இன்னும் இணைப்பில் இருக்கிறீர்களா? நீங்கள் தயாரானதும் சொல்லுங்கள், நான் இங்கே இருக்கிறேன்.",
    },
    "silence_close": {
        "ml": "ഇപ്പോൾ നിങ്ങൾക്ക് സംസാരിക്കാൻ കഴിയുന്നില്ലെന്ന് തോന്നുന്നു. ഞാൻ പിന്നീട് ബന്ധപ്പെടാം. നന്ദി!",
        "ta": "இப்போது உங்களால் பேச முடியவில்லை என்று தோன்றுகிறது. நான் பிறகு தொடர்பு கொள்கிறேன். நன்றி!",
    },
    "hold_ack": {
        "ml": "തീർച്ചയായും, ഞാൻ ലൈനിൽ ഉണ്ട്. സമയമെടുത്തോളൂ.",
        "ta": "கண்டிப்பாக, நான் இணைப்பில் இருக்கிறேன். நிதானமாகச் செய்யுங்கள்.",
    },
    "repeat_none": {
        "ml": "ഞാൻ ഇതുവരെ ഒന്നും പറഞ്ഞിട്ടില്ല.",
        "ta": "நான் இன்னும் எதுவும் சொல்லவில்லை.",
    },
    "slower_ack": {
        "ml": "തീർച്ചയായും, ഞാൻ പതുക്കെ പറയാം. നിങ്ങൾക്ക് എന്താണ് അറിയേണ്ടത്?",
        "ta": "கண்டிப்பாக, மெதுவாகச் சொல்கிறேன். நீங்கள் என்ன தெரிந்துகொள்ள விரும்புகிறீர்கள்?",
    },
    "ack": {
        "ml": "ശരി.",
        "ta": "சரி.",
    },
    # After unclear STT on the greeting's pending question (entry_question_retry).
    "entry_retry_prefix": {
        "en": "Sorry, I couldn't understand that. ",
        "hi": "माफ़ कीजिए, मैं आपकी बात ठीक से समझ नहीं पाया। ",
        "ml": "ക്ഷമിക്കണം, അത് ശരിയായി മനസ്സിലായില്ല. ",
        "ta": "மன்னிக்கவும், அது சரியாகப் புரியவில்லை. ",
    },
    "wf_retry_prefix": {
        "ml": "ക്ഷമിക്കണം, മനസ്സിലായില്ല. ",
        "ta": "மன்னிக்கவும், புரியவில்லை. ",
    },
    "wf_more_detail": {
        "ml": "കുറച്ചുകൂടി വിശദമായി പറയാമോ?",
        "ta": "இன்னும் கொஞ்சம் விளக்கமாகச் சொல்ல முடியுமா?",
    },
    "wf_digits_partial": {
        "ml": "ശരി, ഇതുവരെയുള്ള അക്കങ്ങൾ കുറിച്ചെടുത്തു — ദയവായി തുടരൂ.",
        "ta": "சரி, இதுவரை உள்ள இலக்கங்களைக் குறித்துக்கொண்டேன் — தயவுசெய்து தொடருங்கள்.",
    },
    "wf_digits_partial_count": {
        "ml": "ഇതുവരെ {count} അക്കങ്ങൾ കുറിച്ചെടുത്തു — ദയവായി തുടരൂ.",
        "ta": "இதுவரை {count} இலக்கங்களைக் குறித்துக்கொண்டேன் — தயவுசெய்து தொடருங்கள்.",
    },
    "wf_digits_restart": {
        "ml": "ശരി, വീണ്ടും തുടങ്ങാം — ദയവായി മുഴുവൻ നമ്പറും ഒന്നുകൂടി പറയൂ.",
        "ta": "சரி, மீண்டும் தொடங்குவோம் — தயவுசெய்து முழு எண்ணையும் மீண்டும் சொல்லுங்கள்.",
    },
    "wf_digits_none": {
        "ml": "ഇതുവരെ ഒരു അക്കവും കുറിച്ചെടുത്തിട്ടില്ല — ദയവായി നമ്പർ പറയൂ.",
        "ta": "இன்னும் எந்த இலக்கமும் குறிக்கப்படவில்லை — தயவுசெய்து எண்ணைச் சொல்லுங்கள்.",
    },
    "wf_digits_readback": {
        "ml": ("ഇതുവരെ {count} അക്കങ്ങൾ കുറിച്ചെടുത്തു: {digits}. ദയവായി തുടരൂ, "
               "അല്ലെങ്കിൽ വീണ്ടും തുടങ്ങാൻ 'വീണ്ടും' എന്ന് പറയൂ."),
        "ta": ("இதுவரை {count} இலக்கங்களைக் குறித்துக்கொண்டேன்: {digits}. தயவுசெய்து "
               "தொடருங்கள், அல்லது மீண்டும் தொடங்க 'மறுபடியும்' என்று சொல்லுங்கள்."),
    },
    "wf_digits_readback_masked": {
        "ml": ("ഇതുവരെ {count} അക്കങ്ങൾ കുറിച്ചെടുത്തു, അവസാനം {digits}. ദയവായി തുടരൂ, "
               "അല്ലെങ്കിൽ വീണ്ടും തുടങ്ങാൻ 'വീണ്ടും' എന്ന് പറയൂ."),
        "ta": ("இதுவரை {count} இலக்கங்களைக் குறித்துக்கொண்டேன், கடைசியில் {digits}. "
               "தயவுசெய்து தொடருங்கள், அல்லது மீண்டும் தொடங்க 'மறுபடியும்' என்று சொல்லுங்கள்."),
    },
    "wf_digits_overflow": {
        "ml": ("ഈ നമ്പറിൽ ഉള്ളതിനേക്കാൾ കൂടുതൽ അക്കങ്ങളായി — വീണ്ടും തുടങ്ങാം. "
               "ദയവായി മുഴുവൻ നമ്പറും ഒന്നുകൂടി പറയൂ."),
        "ta": ("இந்த எண்ணில் இருக்க வேண்டியதை விட அதிக இலக்கங்கள் வந்துவிட்டன — மீண்டும் "
               "தொடங்குவோம். தயவுசெய்து முழு எண்ணையும் மீண்டும் சொல்லுங்கள்."),
    },
    "wf_repeat": {
        "ml": "ഒന്നുകൂടി പറയാമോ?",
        "ta": "மீண்டும் சொல்ல முடியுமா?",
    },
    "wf_how_help": {
        "ml": "പറയൂ, ഇന്ന് ഞാൻ നിങ്ങളെ എങ്ങനെ സഹായിക്കണം?",
        "ta": "சொல்லுங்கள், இன்று நான் உங்களுக்கு எப்படி உதவலாம்?",
    },
    "wf_anything_else": {
        "ml": "മറ്റെന്തെങ്കിലും കാര്യത്തിൽ ഞാൻ സഹായിക്കണോ?",
        "ta": "வேறு ஏதாவது விஷயத்தில் நான் உதவ வேண்டுமா?",
    },
    "wf_kb_miss": {
        "ml": "ക്ഷമിക്കണം, ആ വിവരം എന്റെ പക്കൽ ഇല്ല.",
        "ta": "மன்னிக்கவும், அந்தத் தகவல் என்னிடம் இல்லை.",
    },
    "wf_handover": {
        "ml": "ക്ഷമിക്കണം, അത് ശരിയായി മനസ്സിലാക്കാൻ കഴിയുന്നില്ല. നിങ്ങളെ ഞങ്ങളുടെ ഏജന്റുമായി ബന്ധിപ്പിക്കുകയാണ്.",
        "ta": "மன்னிக்கவும், அதைச் சரியாகப் புரிந்துகொள்ள முடியவில்லை. உங்களை எங்கள் ஏஜென்டுடன் இணைக்கிறேன்.",
    },
    "wf_error": {
        "ml": "ഈ പ്രക്രിയയിൽ ഒരു പിശക് സംഭവിച്ചു. നിങ്ങളെ ഞങ്ങളുടെ ഏജന്റുമായി ബന്ധിപ്പിക്കുകയാണ്.",
        "ta": "இந்த செயல்முறையில் ஒரு பிழை ஏற்பட்டது. உங்களை எங்கள் ஏஜென்டுடன் இணைக்கிறேன்.",
    },
    "wf_missing": {
        "ml": "ക്ഷമിക്കണം, ആ പ്രക്രിയ ഇപ്പോൾ തുടങ്ങാൻ കഴിയുന്നില്ല. നിങ്ങളെ ഞങ്ങളുടെ ഏജന്റുമായി ബന്ധിപ്പിക്കുകയാണ്.",
        "ta": "மன்னிக்கவும், அந்த செயல்முறையை இப்போது தொடங்க முடியவில்லை. உங்களை எங்கள் ஏஜென்டுடன் இணைக்கிறேன்.",
    },
    "wf_timeout": {
        "ml": "ക്ഷമിക്കണം, ഇതിന് പ്രതീക്ഷിച്ചതിലും കൂടുതൽ സമയമെടുത്തു. നിങ്ങളെ ഞങ്ങളുടെ ഏജന്റുമായി ബന്ധിപ്പിക്കുകയാണ്.",
        "ta": "மன்னிக்கவும், இதற்கு எதிர்பார்த்ததை விட அதிக நேரம் ஆகிவிட்டது. உங்களை எங்கள் ஏஜென்டுடன் இணைக்கிறேன்.",
    },
}
for _key, _extra in _PHRASES_ML_TA.items():
    _PHRASES.setdefault(_key, {}).update(_extra)


def entry_question(greeting: str) -> str:
    """Extract the authored opening question without an apology or identity."""
    import re

    sentences = re.split(r"(?<=[।.!?？؟])\s*", greeting or "")
    return next((part.strip() for part in reversed(sentences)
                 if part.strip().endswith(("?", "？", "؟"))), "")


def entry_question_retry(greeting: str, locale: str | None = None) -> str:
    """Repeat only the authored greeting's pending question after unclear STT."""
    question = entry_question(greeting)
    if not question:
        return canned("clarify", locale)
    return canned("entry_retry_prefix", locale) + question
